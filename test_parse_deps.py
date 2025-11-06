#!/usr/bin/env python
"""
Parse every *.scala, *.java, and *.py file in ../the-algorithm using tree-sitter-languages.
Build dependency graphs showing which files depend on which other files for each language.

UPDATED: Python import resolution now uses canonical rule-based approach.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Generator, Dict, List, Set, Tuple, Optional
from collections import defaultdict
from dataclasses import dataclass

import tree_sitter_languages
from tree_sitter import Node
import traceback


# ============================================================================
# COMMON UTILITIES
# ============================================================================

def walk_files_by_extension(root: Path, extension: str) -> Generator[Path, None, None]:
    """Yield every file with given extension under *root* (recursive)."""
    for dirpath, _, filenames in os.walk(root):
        # Skip test/ or tests/ directories
        if 'test' in dirpath.split(os.sep) or 'tests' in dirpath.split(os.sep):
            continue
        for filename in filenames:
            if filename.endswith(extension):
                yield Path(dirpath) / filename


def canonicalize_path(path: Path, repo_root: Path) -> str:
    """
    Canonicalize a file path relative to repo root.
    This resolves symlinks and normalizes the path format.
    """
    try:
        # Resolve symlinks and normalize
        absolute_path = path.resolve()
        repo_root_resolved = repo_root.resolve()
        
        # Get relative path with forward slashes (POSIX style)
        relative = absolute_path.relative_to(repo_root_resolved)
        return relative.as_posix()
    except (ValueError, OSError) as e:
        # Fallback to simple relative path if resolution fails
        return path.relative_to(repo_root).as_posix()

# ============================================================================
# SCALA PARSER
# ============================================================================

def extract_scala_package_name(tree, source_bytes: bytes) -> str:
    """Extract the package declaration from a Scala parse tree."""
    root_node = tree.root_node
    
    for child in root_node.children:
        if child.type == "package_clause":
            for subchild in child.children:
                if subchild.type == "package_identifier" or subchild.type == "stable_identifier":
                    package_name = source_bytes[subchild.start_byte:subchild.end_byte].decode('utf-8')
                    return package_name
    
    return ""


def extract_scala_class_names(tree, source_bytes: bytes) -> List[str]:
    """Extract class, object, and trait names from a Scala parse tree."""
    names = []
    
    def traverse(node: Node):
        if node.type in ["class_definition", "object_definition", "trait_definition"]:
            for child in node.children:
                if child.type == "identifier":
                    name = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                    names.append(name)
                    break
        
        for child in node.children:
            traverse(child)
    
    traverse(tree.root_node)
    return names


def extract_scala_imports(tree, source_bytes: bytes) -> List[str]:
    """Extract import statements from a Scala parse tree."""
    imports = []
    
    def traverse(node: Node):
        if node.type == "import_declaration":
            import_text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
            imports.append(import_text)
        
        for child in node.children:
            traverse(child)
    
    traverse(tree.root_node)
    return imports


def should_skip_import(import_name: str) -> bool:
    """
    Check if import is from standard library or common external dependencies.
    These should not be resolved within the repository.
    """
    skip_prefixes = [
        "scala.",
        "java.",
        "javax.",
        "kotlin.",
        "org.scala-lang.",
        "akka.",           # Common Scala framework
        "play.",           # Play Framework
        "cats.",           # Cats library
        "scalaz.",         # Scalaz library
    ]
    return any(import_name.startswith(prefix) for prefix in skip_prefixes)


def parse_scala_import(import_stmt: str) -> Tuple[List[str], bool]:
    """
    Parse a Scala import statement and return imported names.
    
    Returns:
        Tuple of (list of imported fully-qualified names, is_wildcard)
        
    Examples:
        "import com.example.Foo" -> (["com.example.Foo"], False)
        "import com.example._" -> (["com.example"], True)
        "import com.example.{Foo, Bar}" -> (["com.example.Foo", "com.example.Bar"], False)
        "import com.example.{Foo => Bar}" -> (["com.example.Foo"], False)
        "import com.example.{Foo => _, _}" -> (["com.example"], True)
    """
    import_stmt = import_stmt.strip()
    if not import_stmt.startswith("import "):
        return [], False
    
    import_path = import_stmt[7:].strip()
    
    # Handle wildcard imports: import com.example._
    if import_path.endswith("._"):
        base_path = import_path[:-2]
        return [base_path], True
    
    # Handle selective imports: import com.example.{Foo, Bar, Baz => Renamed}
    if "{" in import_path and "}" in import_path:
        base_path = import_path[:import_path.index("{")].strip().rstrip(".")
        items = import_path[import_path.index("{")+1:import_path.index("}")].strip()
        
        imported_names = []
        has_wildcard = False
        
        for item in items.split(","):
            item = item.strip()
            if not item:
                continue
            
            # Handle renaming: Foo => Bar or exclusion: Foo => _
            if "=>" in item:
                parts = item.split("=>")
                original = parts[0].strip()
                renamed = parts[1].strip()
                
                # Check for exclusion pattern (=> _)
                if renamed == "_":
                    # This is an exclusion, skip it
                    continue
                elif original == "_":
                    # Pattern: {_ => _, _} means exclude and import rest
                    # This is rare, treat as wildcard
                    has_wildcard = True
                else:
                    # Normal renaming, we care about the original name
                    imported_names.append(f"{base_path}.{original}")
            elif item == "_":
                # Wildcard import: {Foo, Bar, _}
                has_wildcard = True
            else:
                # Simple import: {Foo}
                imported_names.append(f"{base_path}.{item}")
        
        return imported_names, has_wildcard
    
    # Simple import: import com.example.Foo
    return [import_path], False


@dataclass
class ScalaIndex:
    """Index of Scala classes, objects, traits, and packages."""
    # Maps fully-qualified class name to canonical file path
    class_to_file: Dict[str, str]
    
    # Maps package name to list of canonical file paths in that package
    package_to_files: Dict[str, List[str]]
    
    # Maps package name to list of classes defined in that package
    package_to_classes: Dict[str, List[str]]


def build_scala_index(repo_root: Path, parser) -> ScalaIndex:
    """Build an index mapping Scala fully-qualified class names to file paths."""
    class_to_file = {}
    package_to_files = defaultdict(list)
    package_to_classes = defaultdict(list)
    
    for scala_file in walk_files_by_extension(repo_root, ".scala"):
        try:
            source = scala_file.read_bytes()
            tree = parser.parse(source)
            
            package_name = extract_scala_package_name(tree, source)
            class_names = extract_scala_class_names(tree, source)
            
            # Use canonicalized path
            canonical_path = canonicalize_path(scala_file, repo_root)
            
            # Index each class/object/trait
            for class_name in class_names:
                if package_name:
                    fqn = f"{package_name}.{class_name}"
                else:
                    fqn = class_name
                
                class_to_file[fqn] = canonical_path
                
                if package_name:
                    package_to_classes[package_name].append(class_name)
            
            # Index package membership
            if package_name:
                if canonical_path not in package_to_files[package_name]:
                    package_to_files[package_name].append(canonical_path)
        
        except Exception as exc:
            print(f"Warning: Failed to index {scala_file}: {exc}", file=sys.stderr)
            traceback.print_exc()
    
    return ScalaIndex(
        class_to_file=class_to_file,
        package_to_files=dict(package_to_files),
        package_to_classes=dict(package_to_classes)
    )


def resolve_import_to_files(
    import_name: str,
    is_wildcard: bool,
    index: ScalaIndex,
    current_file: str
) -> Set[str]:
    """
    Resolve an imported name to the set of files it depends on.
    
    Args:
        import_name: The fully-qualified import name
        is_wildcard: Whether this is a wildcard import
        index: The ScalaIndex containing class and package information
        current_file: The canonical path of the file doing the importing
        
    Returns:
        Set of canonical file paths that this import depends on
    """
    dependent_files = set()
    
    # Skip standard library and external dependencies
    if should_skip_import(import_name):
        return dependent_files
    
    if is_wildcard:
        # Wildcard import: import com.example._
        # Add all files in the package
        if import_name in index.package_to_files:
            for file_path in index.package_to_files[import_name]:
                if file_path != current_file:
                    dependent_files.add(file_path)
    else:
        # Specific import: import com.example.Foo
        
        # Direct class/object/trait match
        if import_name in index.class_to_file:
            file_path = index.class_to_file[import_name]
            if file_path != current_file:
                dependent_files.add(file_path)
        # Check if it's a package (for package object or nested imports)
        elif import_name in index.package_to_files:
            for file_path in index.package_to_files[import_name]:
                if file_path != current_file:
                    dependent_files.add(file_path)
    
    return dependent_files


def resolve_scala_dependencies(
    repo_root: Path,
    parser,
    index: ScalaIndex
) -> Dict[str, List[str]]:
    """
    Build the dependency graph for Scala files.
    
    Returns a mapping from canonical file path to list of canonical file paths
    it depends on (sorted for consistency).
    """
    dependencies = defaultdict(set)
    
    for scala_file in walk_files_by_extension(repo_root, ".scala"):
        try:
            source = scala_file.read_bytes()
            tree = parser.parse(source)
            
            canonical_path = canonicalize_path(scala_file, repo_root)
            dependencies[canonical_path] = dependencies.get(canonical_path, set())
            imports = extract_scala_imports(tree, source)
            
            # Resolve each import
            for import_stmt in imports:
                imported_names, is_wildcard = parse_scala_import(import_stmt)
                
                for import_name in imported_names:
                    resolved_files = resolve_import_to_files(
                        import_name,
                        is_wildcard,
                        index,
                        canonical_path
                    )
                    dependencies[canonical_path].update(resolved_files)
        
        except Exception as exc:
            print(f"Warning: Failed to resolve dependencies for {scala_file}: {exc}", file=sys.stderr)
            traceback.print_exc()
    
    # Convert sets to sorted lists for consistent output
    return {
        file_path: sorted(list(deps))
        for file_path, deps in dependencies.items()
    }

# ============================================================================
# JAVA PARSER
# ============================================================================

def extract_java_package_name(tree, source_bytes: bytes) -> str:
    """Extract the package declaration from a Java parse tree."""
    root_node = tree.root_node
    
    for child in root_node.children:
        if child.type == "package_declaration":
            for subchild in child.children:
                if subchild.type == "scoped_identifier" or subchild.type == "identifier":
                    package_name = source_bytes[subchild.start_byte:subchild.end_byte].decode('utf-8')
                    return package_name
    
    return ""


def extract_java_class_names(tree, source_bytes: bytes) -> List[str]:
    """
    Extract class, interface, enum, and record names from a Java parse tree.
    Returns both top-level and nested class names.
    """
    names = []
    
    def traverse(node: Node, parent_class: Optional[str] = None):
        class_types = [
            "class_declaration",
            "interface_declaration", 
            "enum_declaration",
            "record_declaration",  # Java 14+
            "annotation_type_declaration"
        ]
        
        if node.type in class_types:
            for child in node.children:
                if child.type == "identifier":
                    class_name = source_bytes[child.start_byte:child.end_byte].decode('utf-8')
                    
                    # For nested classes, use OuterClass.InnerClass notation
                    if parent_class:
                        full_name = f"{parent_class}.{class_name}"
                    else:
                        full_name = class_name
                    
                    names.append(full_name)
                    
                    # Recursively find nested classes
                    for subchild in node.children:
                        if subchild.type == "class_body":
                            traverse(subchild, full_name)
                    break
        else:
            for child in node.children:
                traverse(child, parent_class)
    
    traverse(tree.root_node)
    return names


def extract_java_imports(tree, source_bytes: bytes) -> List[str]:
    """Extract import statements from a Java parse tree."""
    imports = []
    
    def traverse(node: Node):
        if node.type == "import_declaration":
            import_text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
            imports.append(import_text)
        
        for child in node.children:
            traverse(child)
    
    traverse(tree.root_node)
    return imports


def extract_java_type_references(tree, source_bytes: bytes) -> Set[str]:
    """
    Extract type references from Java code (for same-package dependency detection).
    This helps identify dependencies that don't require imports.
    """
    type_refs = set()
    
    def traverse(node: Node):
        # Look for type identifiers in various contexts
        if node.type == "type_identifier":
            type_name = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
            type_refs.add(type_name)
        
        for child in node.children:
            traverse(child)
    
    traverse(tree.root_node)
    return type_refs


def should_skip_import(import_name: str) -> bool:
    """
    Check if import is from standard library or common external dependencies.
    These should not be resolved within the repository.
    """
    skip_prefixes = [
        "java.",
        "javax.",
        "jakarta.",      # Jakarta EE (formerly Java EE)
        "jdk.",
        "sun.",
        "com.sun.",
        "org.xml.",
        "org.w3c.",
        "org.ietf.",
        "org.omg.",
        # Common third-party libraries (optional - adjust as needed)
        "org.springframework.",
        "org.apache.commons.",
        "com.google.common.",  # Guava
        "org.junit.",
        "org.mockito.",
        "org.slf4j.",
        "ch.qos.logback.",
        "org.hibernate.",
    ]
    return any(import_name.startswith(prefix) for prefix in skip_prefixes)


def parse_java_import(import_stmt: str) -> Tuple[List[str], bool, bool]:
    """
    Parse a Java import statement and return imported names.
    
    Returns:
        Tuple of (list of imported names, is_wildcard, is_static)
        
    Examples:
        "import com.example.Foo;" -> (["com.example.Foo"], False, False)
        "import com.example.*;" -> (["com.example"], True, False)
        "import static com.example.Utils.*;" -> (["com.example.Utils"], True, True)
        "import static com.example.Utils.helper;" -> (["com.example.Utils"], False, True)
    """
    import_stmt = import_stmt.strip()
    if not import_stmt.startswith("import "):
        return [], False, False
    
    import_path = import_stmt[7:].strip()
    is_static = False
    
    # Handle static imports
    if import_path.startswith("static "):
        is_static = True
        import_path = import_path[7:].strip()
    
    # Remove trailing semicolon
    if import_path.endswith(";"):
        import_path = import_path[:-1].strip()
    
    # Handle wildcard imports
    if import_path.endswith(".*"):
        package_or_class = import_path[:-2]
        return [package_or_class], True, is_static
    
    # For static imports of specific members, we need the class, not the member
    if is_static and "." in import_path:
        # import static com.example.Utils.helper -> we care about com.example.Utils
        parts = import_path.rsplit(".", 1)
        class_name = parts[0]
        return [class_name], False, is_static
    
    return [import_path], False, is_static


@dataclass
class JavaIndex:
    """Index of Java classes, interfaces, enums, and packages."""
    # Maps fully-qualified class name (including nested classes) to canonical file path
    class_to_file: Dict[str, str]
    
    # Maps package name to list of canonical file paths in that package
    package_to_files: Dict[str, List[str]]
    
    # Maps package name to list of top-level classes defined in that package
    package_to_classes: Dict[str, List[str]]
    
    # Maps canonical file path to its package name
    file_to_package: Dict[str, str]


def build_java_index(repo_root: Path, parser) -> JavaIndex:
    """Build an index mapping Java fully-qualified class names to file paths."""
    class_to_file = {}
    package_to_files = defaultdict(list)
    package_to_classes = defaultdict(list)
    file_to_package = {}
    
    for java_file in walk_files_by_extension(repo_root, ".java"):
        try:
            source = java_file.read_bytes()
            tree = parser.parse(source)
            
            package_name = extract_java_package_name(tree, source)
            class_names = extract_java_class_names(tree, source)
            
            # Use canonicalized path
            canonical_path = canonicalize_path(java_file, repo_root)
            
            # Index each class/interface/enum (including nested)
            for class_name in class_names:
                if package_name:
                    # For nested classes, class_name already contains the dots
                    fqn = f"{package_name}.{class_name}"
                else:
                    fqn = class_name
                
                class_to_file[fqn] = canonical_path
                
                # Only add top-level classes to package_to_classes
                if package_name and "." not in class_name:
                    package_to_classes[package_name].append(class_name)
            
            # Index package membership
            if package_name:
                if canonical_path not in package_to_files[package_name]:
                    package_to_files[package_name].append(canonical_path)
                file_to_package[canonical_path] = package_name
            else:
                # Default package (no package declaration)
                file_to_package[canonical_path] = ""
        
        except Exception as exc:
            print(f"Warning: Failed to index {java_file}: {exc}", file=sys.stderr)
            traceback.print_exc()
    
    return JavaIndex(
        class_to_file=class_to_file,
        package_to_files=dict(package_to_files),
        package_to_classes=dict(package_to_classes),
        file_to_package=file_to_package
    )


def resolve_import_to_files_for_java(
    import_name: str,
    is_wildcard: bool,
    is_static: bool,
    index: JavaIndex,
    current_file: str
) -> Set[str]:
    """
    Resolve an imported name to the set of files it depends on.
    
    Args:
        import_name: The fully-qualified import name
        is_wildcard: Whether this is a wildcard import
        is_static: Whether this is a static import
        index: The JavaIndex containing class and package information
        current_file: The canonical path of the file doing the importing
        
    Returns:
        Set of canonical file paths that this import depends on
    """
    dependent_files = set()
    
    # Skip standard library and external dependencies
    if should_skip_import(import_name):
        return dependent_files
    
    if is_wildcard:
        if is_static:
            # static wildcard import: import static com.example.Utils.*;
            # This imports all static members from the class
            if import_name in index.class_to_file:
                file_path = index.class_to_file[import_name]
                if file_path != current_file:
                    dependent_files.add(file_path)
        else:
            # Regular wildcard import: import com.example.*;
            # This imports all classes in the package
            if import_name in index.package_to_files:
                for file_path in index.package_to_files[import_name]:
                    if file_path != current_file:
                        dependent_files.add(file_path)
    else:
        # Specific import
        if import_name in index.class_to_file:
            # Direct class match (including nested classes like com.example.Outer.Inner)
            file_path = index.class_to_file[import_name]
            if file_path != current_file:
                dependent_files.add(file_path)
    
    return dependent_files


def resolve_same_package_dependencies(
    source_bytes: bytes,
    tree,
    current_file: str,
    index: JavaIndex
) -> Set[str]:
    """
    Resolve dependencies to classes in the same package (which don't require imports).
    
    In Java, classes in the same package can reference each other without imports.
    """
    dependent_files = set()
    
    # Get current file's package
    current_package = index.file_to_package.get(current_file, "")
    
    # Extract type references from the code
    type_refs = extract_java_type_references(tree, source_bytes)
    
    # For each type reference, check if it's a class in the same package
    for type_ref in type_refs:
        if current_package:
            fqn = f"{current_package}.{type_ref}"
        else:
            fqn = type_ref
        
        if fqn in index.class_to_file:
            file_path = index.class_to_file[fqn]
            if file_path != current_file:
                dependent_files.add(file_path)
    
    return dependent_files


def resolve_java_dependencies(
    repo_root: Path,
    parser,
    index: JavaIndex
) -> Dict[str, List[str]]:
    """
    Build the dependency graph for Java files.
    
    Returns a mapping from canonical file path to list of canonical file paths
    it depends on (sorted for consistency).
    """
    dependencies = defaultdict(set)
    
    for java_file in walk_files_by_extension(repo_root, ".java"):
        try:
            source = java_file.read_bytes()
            tree = parser.parse(source)
            
            canonical_path = canonicalize_path(java_file, repo_root)
            dependencies[canonical_path] = dependencies.get(canonical_path, set())
            imports = extract_java_imports(tree, source)
            
            # Resolve explicit imports
            for import_stmt in imports:
                imported_names, is_wildcard, is_static = parse_java_import(import_stmt)
                
                for import_name in imported_names:
                    resolved_files = resolve_import_to_files_for_java(
                        import_name,
                        is_wildcard,
                        is_static,
                        index,
                        canonical_path
                    )
                    dependencies[canonical_path].update(resolved_files)
            
            # Resolve same-package dependencies (implicit imports)
            same_package_deps = resolve_same_package_dependencies(
                source,
                tree,
                canonical_path,
                index
            )
            dependencies[canonical_path].update(same_package_deps)
        
        except Exception as exc:
            print(f"Warning: Failed to resolve dependencies for {java_file}: {exc}", file=sys.stderr)
            traceback.print_exc()
    
    # Convert sets to sorted lists for consistent output
    return {
        file_path: sorted(list(deps))
        for file_path, deps in dependencies.items()
    }


# ============================================================================
# PYTHON PARSER - UPDATED WITH CANONICAL RULE-BASED RESOLUTION
# ============================================================================

@dataclass
class PythonImport:
    """Represents a Python import statement."""
    type: str  # 'import' or 'from'
    module: str  # The module being imported
    names: List[str]  # Names imported (empty for 'import' statements)


class PythonModuleIndex:
    """
    Builds a canonical mapping of Python module paths to file paths.
    
    Rules:
    1. A directory with __init__.py is a package
    2. A .py file is a module
    3. Module path = file path with / replaced by . and .py removed
    4. __init__.py represents the parent directory
    """
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.module_to_file: Dict[str, Path] = {}
        self.file_to_module: Dict[Path, str] = {}
        self._build_index()
    
    def _build_index(self):
        """Build the complete module index."""
        for python_file in walk_files_by_extension(self.repo_root, ".py"):
            module_path = self._file_to_module_path(python_file)
            if module_path:
                # Store both directions
                self.module_to_file[module_path] = python_file
                self.file_to_module[python_file] = module_path
                
                # Also index parent packages
                self._index_parent_packages(python_file, module_path)
    
    def _file_to_module_path(self, file_path: Path) -> Optional[str]:
        """
        Convert a file path to a module path.
        
        Examples:
            myproject/module_a.py -> myproject.module_a
            myproject/__init__.py -> myproject
            setup.py -> setup
        """
        try:
            relative = file_path.relative_to(self.repo_root)
        except ValueError:
            return None
        
        parts = list(relative.parts)
        
        # Remove .py extension
        if parts[-1].endswith('.py'):
            parts[-1] = parts[-1][:-3]
        
        # If it's __init__, remove it (the directory IS the package)
        if parts[-1] == '__init__':
            parts = parts[:-1]
        
        # If we have no parts left, skip
        if not parts:
            return None
        
        return '.'.join(parts)
    
    def _index_parent_packages(self, file_path: Path, module_path: str):
        """Index parent packages if they have __init__.py files."""
        parts = module_path.split('.')
        current_path = self.repo_root
        
        for i in range(len(parts) - 1):
            current_path = current_path / parts[i]
            init_file = current_path / '__init__.py'
            
            if init_file.exists():
                parent_module = '.'.join(parts[:i+1])
                if parent_module not in self.module_to_file:
                    self.module_to_file[parent_module] = init_file
                    self.file_to_module[init_file] = parent_module
    
    def get_file_for_module(self, module_path: str) -> Optional[Path]:
        """Get the file path for a module path."""
        return self.module_to_file.get(module_path)
    
    def get_module_for_file(self, file_path: Path) -> Optional[str]:
        """Get the module path for a file path."""
        return self.file_to_module.get(file_path)


class PythonImportExtractor:
    """Extract import statements using tree-sitter."""
    
    def __init__(self):
        self.parser = tree_sitter_languages.get_parser("python")
    
    def extract_imports(self, source_bytes: bytes) -> List[PythonImport]:
        """Extract all import statements from Python source code."""
        tree = self.parser.parse(source_bytes)
        imports = []
        
        def traverse(node: Node):
            if node.type == "import_statement":
                imports.extend(self._parse_import_statement(node, source_bytes))
            elif node.type == "import_from_statement":
                import_stmt = self._parse_from_statement(node, source_bytes)
                if import_stmt:
                    imports.append(import_stmt)
            
            for child in node.children:
                traverse(child)
        
        traverse(tree.root_node)
        return imports
    
    def _parse_import_statement(self, node: Node, source_bytes: bytes) -> List[PythonImport]:
        """Parse: import module_a, module_b as alias"""
        imports = []
        
        for child in node.children:
            if child.type == "dotted_name":
                module = self._get_text(child, source_bytes)
                imports.append(PythonImport('import', module, []))
            elif child.type == "aliased_import":
                for subchild in child.children:
                    if subchild.type == "dotted_name":
                        module = self._get_text(subchild, source_bytes)
                        imports.append(PythonImport('import', module, []))
                        break
        
        return imports
    
    def _parse_from_statement(self, node: Node, source_bytes: bytes) -> Optional[PythonImport]:
        """
        Parse: from module import name1, name2
               from . import name
               from .. import name
               from ..module import name
        
        CRITICAL FIX: Now handles identifier nodes for simple imports like 'from dictquery import X'
        """
        module_name = None
        imported_names = []
        
        for child in node.children:
            # Get the module path - KEY FIX: handle identifier nodes!
            if child.type == "dotted_name" and module_name is None:
                module_name = self._get_text(child, source_bytes)
            elif child.type == "relative_import":
                module_name = self._get_text(child, source_bytes)
            elif child.type == "identifier" and module_name is None:
                # CRITICAL FIX: Handle simple identifiers like 'dictquery'
                text = self._get_text(child, source_bytes)
                if text not in ['from', 'import', 'as']:
                    module_name = text
            
            # Get imported names
            elif child.type == "wildcard_import":
                imported_names.append("*")
            elif child.type == "dotted_name" and module_name is not None:
                imported_names.append(self._get_text(child, source_bytes))
            elif child.type == "identifier" and module_name is not None:
                text = self._get_text(child, source_bytes)
                if text not in ['from', 'import', 'as']:
                    imported_names.append(text)
            elif child.type == "aliased_import":
                for subchild in child.children:
                    if subchild.type in ["identifier", "dotted_name"]:
                        text = self._get_text(subchild, source_bytes)
                        if text not in ['as']:
                            imported_names.append(text)
                            break
        
        if module_name:
            return PythonImport('from', module_name, imported_names)
        return None
    
    def _get_text(self, node: Node, source_bytes: bytes) -> str:
        """Extract text from a node."""
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8')


class PythonImportResolver:
    """Resolve imports to actual file paths using Python's import resolution rules."""
    
    def __init__(self, module_index: PythonModuleIndex):
        self.index = module_index
    
    def resolve_absolute_import(self, module_path: str) -> Optional[Path]:
        """
        Resolve an absolute import to a file path.
        
        Examples:
            'myproject' -> myproject/__init__.py
            'myproject.module_a' -> myproject/module_a.py
        """
        return self.index.get_file_for_module(module_path)
    
    def resolve_relative_import(self, current_file: Path, relative_import: str) -> Optional[Path]:
        """
        Resolve a relative import to a file path.
        
        Examples:
            From myproject/subpackage/module_b.py:
            '.' -> myproject/subpackage/__init__.py
            '..' -> myproject/__init__.py
            '..module_a' -> myproject/module_a.py
        """
        # Count the dots
        dots = 0
        for char in relative_import:
            if char == '.':
                dots += 1
            else:
                break
        
        # Get current module path
        current_module = self.index.get_module_for_file(current_file)
        if not current_module:
            return None
        
        # Split into parts
        current_parts = current_module.split('.')
        
        # Calculate how many levels to go up
        # . = current package (0 levels up)
        # .. = parent package (1 level up)
        # ... = grandparent (2 levels up)
        levels_up = dots - 1
        
        if levels_up >= len(current_parts):
            return None
        
        # Get the target package
        if levels_up > 0:
            target_parts = current_parts[:-levels_up]
        else:
            target_parts = current_parts
        
        # Add the remaining path
        remaining = relative_import[dots:].strip('.')
        if remaining:
            target_parts.extend(remaining.split('.'))
        
        # Convert back to module path
        target_module = '.'.join(target_parts) if target_parts else None
        
        if target_module:
            return self.index.get_file_for_module(target_module)
        return None
    
    def resolve_import(self, import_stmt: PythonImport, current_file: Path) -> Set[Path]:
        """
        Resolve an import statement to file paths.
        
        Returns all files that this import depends on.
        """
        dependencies = set()
        
        module = import_stmt.module
        
        # Check if it's a relative import
        if module.startswith('.'):
            resolved = self.resolve_relative_import(current_file, module)
            if resolved and resolved != current_file:
                dependencies.add(resolved)
            
            # FIX: Also check for submodule imports in relative imports
            if import_stmt.type == 'from' and import_stmt.names:
                for name in import_stmt.names:
                    if name != '*':
                        # Construct relative submodule path
                        submodule = f"{module}.{name}"
                        sub_resolved = self.resolve_relative_import(current_file, submodule)
                        if sub_resolved and sub_resolved != current_file:
                            dependencies.add(sub_resolved)
        else:
            # Absolute import
            resolved = self.resolve_absolute_import(module)
            if resolved and resolved != current_file:
                dependencies.add(resolved)
            
            # Also check for submodule imports
            if import_stmt.type == 'from' and import_stmt.names:
                for name in import_stmt.names:
                    if name != '*':
                        submodule = f"{module}.{name}"
                        sub_resolved = self.resolve_absolute_import(submodule)
                        if sub_resolved and sub_resolved != current_file:
                            dependencies.add(sub_resolved)
        
        return dependencies


def build_python_index(repo_root: Path) -> PythonModuleIndex:
    """Build an index mapping Python module paths to file paths."""
    return PythonModuleIndex(repo_root)


def resolve_python_dependencies(repo_root: Path, module_index: PythonModuleIndex) -> Dict[str, List[str]]:
    """Build the dependency graph for Python files."""
    dependencies = defaultdict(set)
    extractor = PythonImportExtractor()
    resolver = PythonImportResolver(module_index)
    
    for python_file in walk_files_by_extension(repo_root, ".py"):
        try:
            source = python_file.read_bytes()
            imports = extractor.extract_imports(source)
            
            relative_path = canonicalize_path(python_file, repo_root)
            dependencies[relative_path] = dependencies.get(relative_path, set())
            
            for import_stmt in imports:
                resolved_files = resolver.resolve_import(import_stmt, python_file)
                for dep_file in resolved_files:
                    dep_relative = dep_file.relative_to(repo_root).as_posix()
                    dependencies[relative_path].add(dep_relative)
        
        except Exception as exc:
            print(f"Warning: Failed to resolve dependencies for {python_file}: {exc}")
    
    # Convert sets to sorted lists
    return {path: sorted(list(deps)) for path, deps in dependencies.items()}


# ============================================================================
# MAIN
# ============================================================================

def parse_dependencies(repo_root: Path) -> Dict[str, List[str]]:
    
    if not repo_root.is_dir():
        print(f"ERROR: '{repo_root.name}' not found")
        sys.exit(1)

    all_dependencies = {}
    
    # Process Scala files
    print("=" * 60)
    print("PROCESSING SCALA FILES")
    print("=" * 60)
    scala_parser = tree_sitter_languages.get_parser("scala")
    print("Building Scala class-to-file index...")
    scala_index = build_scala_index(repo_root, scala_parser)
    print(f"Indexed {len(scala_index.class_to_file)} classes/objects/traits")
    print(f"Found {len(scala_index.package_to_files)} packages")
    print("Resolving Scala dependencies...")
    scala_deps = resolve_scala_dependencies(repo_root, scala_parser, scala_index)
    print(f"  Found dependencies for {len(scala_deps)} Scala files")
    scala_total = sum(len(deps) for deps in scala_deps.values())
    scala_with_deps = sum(1 for deps in scala_deps.values() if deps)
    print(f"  Files with dependencies: {scala_with_deps}")
    print(f"  Total dependency edges: {scala_total}")
    all_dependencies.update(scala_deps)
    
    # Process Java files
    print("\n" + "=" * 60)
    print("PROCESSING JAVA FILES")
    print("=" * 60)
    java_parser = tree_sitter_languages.get_parser("java")
    print("Building Java class-to-file index...")
    java_index: JavaIndex = build_java_index(repo_root, java_parser)
    print(f"Indexed {len(java_index.class_to_file)} classes/interfaces/enums")
    print(f"Found {len(java_index.package_to_files)} packages")
    print("Resolving Java dependencies...")
    java_deps = resolve_java_dependencies(repo_root, java_parser, java_index)
    print(f"  Found dependencies for {len(java_deps)} Java files")
    java_total = sum(len(deps) for deps in java_deps.values())
    java_with_deps = sum(1 for deps in java_deps.values() if deps)
    print(f"  Files with dependencies: {java_with_deps}")
    print(f"  Total dependency edges: {java_total}")
    all_dependencies.update(java_deps)
    
    # Process Python files - UPDATED
    print("\n" + "=" * 60)
    print("PROCESSING PYTHON FILES")
    print("=" * 60)
    print("Building Python module-to-file index (canonical rule-based)...")
    python_index = build_python_index(repo_root)
    print(f"  Indexed {len(python_index.module_to_file)} Python modules")
    print("Resolving Python dependencies (using Python's import rules)...")
    python_deps = resolve_python_dependencies(repo_root, python_index)
    print(f"  Found dependencies for {len(python_deps)} Python files")
    python_total = sum(len(deps) for deps in python_deps.values())
    python_with_deps = sum(1 for deps in python_deps.values() if deps)
    print(f"  Files with dependencies: {python_with_deps}")
    print(f"  Total dependency edges: {python_total}")
    all_dependencies.update(python_deps)

    # Write all_dependencies to a JSON file
    output_file = "all_dependencies.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_dependencies, f, indent=2)
    print(f"\nDependency graph written to {output_file}")
    
    return all_dependencies


if __name__ == "__main__":
    repo_path = Path("data/xai-sdk-python")
    parse_dependencies(repo_path)