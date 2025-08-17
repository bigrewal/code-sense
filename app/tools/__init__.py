from .fetch_code_for_def import fetch_code_for_def
from .fetch_code_file import fetch_code_file
from .fetch_cross_file_refs_for_file import fetch_cross_file_refs_for_file
from .lookup_refs_for_def import lookup_refs_for_def
from .lookup_def_usages import lookup_def_usages
from .fetch_entry_point_files import fetch_entry_point_files

# Tool registry for dynamic lookup
TOOL_REGISTRY = {
    "fetch_code_for_def": fetch_code_for_def,
    "fetch_code_file": fetch_code_file,
    "fetch_cross_file_refs_for_file": fetch_cross_file_refs_for_file,
    "lookup_refs_for_def": lookup_refs_for_def,
    "lookup_def_usages": lookup_def_usages,
    "fetch_entry_point_files": fetch_entry_point_files,
}