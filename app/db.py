import logging
from neo4j import GraphDatabase
from pymongo import MongoClient
from .config import Config
from .models.data_model import CodeGraph, ASTNode

from typing import Dict, Tuple
from typing import List, Dict, Any
import time
logger = logging.getLogger(__name__)

## Silence WARNING:neo4j.notifications: 
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

DEF_NODE_TYPES = {"function_definition", "class_definition"}

class Neo4jClient:
    """Neo4j database client wrapper."""

    def __init__(self):
        self.driver = None
        self.batch_size = 1000
        logger.info(f"Initializing Neo4jClient with config: {Config.NEO4J_URI}")
        try:
            self.driver = GraphDatabase.driver(
                Config.NEO4J_URI,
                auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD)
            )
            # Test connection
            with self.driver.session() as session:
                session.run("RETURN 1")
            logger.info("Neo4j connection established successfully")
        except Exception as e:
            logger.warning(f"Neo4j connection failed: {e}")
            raise
    
    async def init_graph_for_repo(self, repo_id: str):
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")
                
        try:
            logger.info(f"Initialising graph for repo: {repo_id}")
            
            with self.driver.session() as session:
                # Create indexes if they don't exist
                self._create_indexes(session)
                
                # Optional: Clear existing data for this repo
                self._clear_repo_data(session, repo_id)
        
        except Exception as exc:
            logger.error(f"Neo4j initialisation faield: {exc}")
            raise

    
    async def batch_create_nodes_and_edges(self, repo_id, nodes: List[ASTNode], edges: List[Dict[str, Any]]):
        try:
            logger.info(f"Initialising graph for repo: {repo_id}")
            
            with self.driver.session() as session:

                # Adding nodes in the graph
                total_node_batches = (len(nodes) + self.batch_size - 1) // self.batch_size
                for i in range(0, len(nodes), self.batch_size):
                    batch_num = (i // self.batch_size) + 1
                    batch = nodes[i:i + self.batch_size]
                    
                    # Convert nodes to dictionaries
                    node_dicts = []
                    for node in batch:
                        node_dict = {
                            'node_id': node.node_id,
                            'node_type': node.node_type,
                            'start_line': node.start_line,
                            'start_column': node.start_column,
                            'end_line': node.end_line,
                            'end_column': node.end_column,
                            'parent_id': node.parent_id,
                            'file_path': node.file_path,
                            'is_definition': node.is_definition,
                            'is_reference': node.is_reference,
                            'repo_id': repo_id,
                            'name': node.name
                        }
                        node_dicts.append(node_dict)
                    
                    query = """
                    UNWIND $nodes AS node
                    CREATE (n:ASTNode)
                    SET n = node
                    """
                    
                    # Add Definition label if is_definition is True
                    query += """
                    WITH n, node
                    WHERE node.is_definition = true
                    SET n:Definition
                    """

                    # Add Reference label if is_reference is True
                    query += """
                    WITH n, node
                    WHERE node.is_reference = true
                    SET n:Reference
                    """

                    try:
                        session.run(query, nodes=node_dicts)
                        logger.debug(f"Created node batch {batch_num}/{total_node_batches} ({len(batch)} nodes)")
                    except Exception as e:
                        logger.error(f"Failed to create node batch {batch_num}: {e}")
                        raise
                
                # Adding edges in the graph
                if not edges:
                    logger.info("No edges to create")
                    return
                
                total_batches = (len(edges) + self.batch_size - 1) // self.batch_size
                
                for i in range(0, len(edges), self.batch_size):
                    batch_num = (i // self.batch_size) + 1
                    batch = edges[i:i + self.batch_size]
                    
                    # Group edges by type for efficient processing
                    contains_edges = []
                    references_edges = []
                    depends_on_edges = []
                    
                    for edge in batch:
                        edge_data = {
                            'source': edge['source'],
                            'target': edge['target'],
                            'sequence': edge.get('sequence', 1)
                        }
                        
                        if edge['type'] == 'CONTAINS':
                            contains_edges.append(edge_data)
                        elif edge['type'] == 'REFERENCES':
                            references_edges.append(edge_data)
                        elif edge['type'] == 'DEPENDS_ON':
                            depends_on_edges.append(edge_data)

                    
                    # Create CONTAINS relationships
                    if contains_edges:
                        contains_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:CONTAINS {sequence: edge.sequence}]->(target)
                        """
                        try:
                            session.run(contains_query, edges=contains_edges)
                            logger.debug(f"Created {len(contains_edges)} CONTAINS edges in batch {batch_num}")
                        except Exception as e:
                            logger.error(f"Failed to create CONTAINS edges in batch {batch_num}: {e}")
                            raise
                    
                    # Create REFERENCES relationships
                    if references_edges:
                        references_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:REFERENCES {sequence: edge.sequence}]->(target)
                        """
                        try:
                            session.run(references_query, edges=references_edges)
                            logger.debug(f"Created {len(references_edges)} REFERENCES edges in batch {batch_num}")
                        except Exception as e:
                            logger.error(f"Failed to create REFERENCES edges in batch {batch_num}: {e}")
                            raise
                    
                    # Create DEPENDS_ON relationships
                    if depends_on_edges:
                        depends_on_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:DEPENDS_ON {sequence: edge.sequence}]->(target)
                        """
                        try:
                            session.run(depends_on_query, edges=depends_on_edges)
                            logger.debug(f"Created {len(depends_on_edges)} DEPENDS_ON edges in batch {batch_num}")
                        except Exception as e:
                            logger.error(f"Failed to create DEPENDS_ON edges in batch {batch_num}: {e}")
                            raise

        except Exception as e:
            print(f"Error occured when trying to add nodes and edges: {e}")
            raise

    async def store_graph(self, code_graph: CodeGraph):
        """Store code graph in Neo4j."""
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")
        
        start_time = time.time()
        
        try:
            logger.info(f"Starting Neo4j storage for repo: {code_graph.repo_id}")
            logger.info(f"Storing {code_graph.total_nodes} nodes and {code_graph.total_edges} edges")
            
            with self.driver.session() as session:
                # Create indexes if they don't exist
                self._create_indexes(session)
                
                # Optional: Clear existing data for this repo
                self._clear_repo_data(session, code_graph.repo_id)
                
                # Store nodes in batches
                self._batch_create_nodes(session, code_graph.nodes, code_graph.repo_id)
                
                # Store edges in batches
                self._batch_create_edges(session, code_graph.edges, code_graph.repo_id)
                
            elapsed_time = time.time() - start_time
            logger.info(f"Neo4j storage completed in {elapsed_time:.2f} seconds")
            
        except Exception as e:
            logger.error(f"Neo4j storage failed: {e}")
            raise

    def _create_indexes(self, session):
        """Create necessary indexes for performance."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.node_id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.repo_id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.file_path)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.node_type)",
        ]
        
        for index_query in indexes:
            try:
                session.run(index_query)
                # logger.debug(f"Created index: {index_query}")
            except Exception as e:
                logger.warning(f"Index creation failed: {e}")
    
    def _clear_repo_data(self, session, repo_id: str):
        """Clear existing data for a repository."""
        try:
            query = """
            MATCH (n:ASTNode {repo_id: $repo_id})
            DETACH DELETE n
            """
            result = session.run(query, repo_id=repo_id)
            logger.info(f"Cleared existing data for repo: {repo_id}")
        except Exception as e:
            logger.warning(f"Failed to clear repo data: {e}")
    
    async def _batch_create_nodes(self, nodes: List[ASTNode], repo_id: str):
        """Create AST nodes in batches."""
        total_batches = (len(nodes) + self.batch_size - 1) // self.batch_size
        
        with self.driver.session() as session:
            for i in range(0, len(nodes), self.batch_size):
                batch_num = (i // self.batch_size) + 1
                batch = nodes[i:i + self.batch_size]
                
                # Convert nodes to dictionaries
                node_dicts = []
                for node in batch:
                    node_dict = {
                        'node_id': node.node_id,
                        'node_type': node.node_type,
                        'start_line': node.start_line,
                        'start_column': node.start_column,
                        'end_line': node.end_line,
                        'end_column': node.end_column,
                        'parent_id': node.parent_id,
                        'file_path': node.file_path,
                        'is_definition': node.is_definition,
                        'is_reference': node.is_reference,
                        'repo_id': repo_id,
                        'name': node.name
                    }
                    node_dicts.append(node_dict)
                
                query = """
                UNWIND $nodes AS node
                CREATE (n:ASTNode)
                SET n = node
                """
                
                # Add Definition label if is_definition is True
                query += """
                WITH n, node
                WHERE node.is_definition = true
                SET n:Definition
                """

                # Add Reference label if is_reference is True
                query += """
                WITH n, node
                WHERE node.is_reference = true
                SET n:Reference
                """

                try:
                    session.run(query, nodes=node_dicts)
                    logger.debug(f"Created node batch {batch_num}/{total_batches} ({len(batch)} nodes)")
                except Exception as e:
                    logger.error(f"Failed to create node batch {batch_num}: {e}")
                    raise
        
        # logger.info(f"Successfully created {len(nodes)} nodes in {total_batches} batches")
    
    async def _batch_create_edges(self, edges: List[Dict[str, Any]], repo_id: str):
        """Create edges in batches."""
        if not edges:
            logger.info("No edges to create")
            return
        
        total_batches = (len(edges) + self.batch_size - 1) // self.batch_size
        
        with self.driver.session() as session:
            for i in range(0, len(edges), self.batch_size):
                batch_num = (i // self.batch_size) + 1
                batch = edges[i:i + self.batch_size]
                
                # Group edges by type for efficient processing
                contains_edges = []
                references_edges = []
                depends_on_edges = []
                
                for edge in batch:
                    edge_data = {
                        'source': edge['source'],
                        'target': edge['target'],
                        'sequence': edge.get('sequence', 1)
                    }
                    
                    if edge['type'] == 'CONTAINS':
                        contains_edges.append(edge_data)
                    elif edge['type'] == 'REFERENCES':
                        references_edges.append(edge_data)
                    elif edge['type'] == 'DEPENDS_ON':
                        depends_on_edges.append(edge_data)

                
                # Create CONTAINS relationships
                if contains_edges:
                    contains_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:CONTAINS {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(contains_query, edges=contains_edges)
                        logger.debug(f"Created {len(contains_edges)} CONTAINS edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create CONTAINS edges in batch {batch_num}: {e}")
                        raise
                
                # Create REFERENCES relationships
                if references_edges:
                    references_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:REFERENCES {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(references_query, edges=references_edges)
                        logger.debug(f"Created {len(references_edges)} REFERENCES edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create REFERENCES edges in batch {batch_num}: {e}")
                        raise
                
                # Create DEPENDS_ON relationships
                if depends_on_edges:
                    depends_on_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:DEPENDS_ON {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(depends_on_query, edges=depends_on_edges)
                        logger.debug(f"Created {len(depends_on_edges)} DEPENDS_ON edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create DEPENDS_ON edges in batch {batch_num}: {e}")
                        raise
                
                # logger.debug(f"Processed edge batch {batch_num}/{total_batches}")
        
        # logger.info(f"Successfully created {len(edges)} edges in {total_batches} batches")
    
    def get_graph_stats(self, repo_id: str) -> Dict[str, Any]:
        """Get statistics about the stored graph."""
        if not self.driver:
            return {}
        
        try:
            with self.driver.session() as session:
                # Count nodes
                node_count = session.run(
                    "MATCH (n:ASTNode {repo_id: $repo_id}) RETURN count(n) as count",
                    repo_id=repo_id
                ).single()['count']
                
                # Count edges
                edge_count = session.run(
                    "MATCH (n:ASTNode {repo_id: $repo_id})-[r]-() RETURN count(r) as count",
                    repo_id=repo_id
                ).single()['count']
                
                # Count definitions
                def_count = session.run(
                    "MATCH (n:ASTNode:Definition {repo_id: $repo_id}) RETURN count(n) as count",
                    repo_id=repo_id
                ).single()['count']
                
                return {
                    'repo_id': repo_id,
                    'total_nodes': node_count,
                    'total_edges': edge_count,
                    'total_definitions': def_count
                }
        except Exception as e:
            logger.error(f"Failed to get graph stats: {e}")
            return {}
    
    def file_dependencies(self, repo_id: str, file_path: str) -> Tuple[List[str], List[str]]:
        """
        Return upstream and downstream file dependencies for a given file.
        Upstream: other files that reference something inside this file.
        Downstream: other files that this file references.
        Uses leaf-level REFERENCES edges between ASTNode nodes and groups by file_path.
        """
        cypher = """
        // Compute upstream (incoming) file dependencies
        CALL {
        WITH $repo_id AS repo_id, $file_path AS fp
        MATCH (leaf:ASTNode {repo_id: repo_id, file_path: fp})
        MATCH (ref_leaf:ASTNode {repo_id: repo_id})-[:REFERENCES]->(leaf)
        WHERE ref_leaf.file_path <> fp
        RETURN COLLECT(DISTINCT ref_leaf.file_path) AS upstream_paths
        }

        // Compute downstream (outgoing) file dependencies
        CALL {
        WITH $repo_id AS repo_id, $file_path AS fp
        MATCH (leaf:ASTNode {repo_id: repo_id, file_path: fp})
        MATCH (leaf)-[:REFERENCES]->(tgt_leaf:ASTNode {repo_id: repo_id})
        WHERE tgt_leaf.file_path <> fp
        RETURN COLLECT(DISTINCT tgt_leaf.file_path) AS downstream_paths
        }

        RETURN upstream_paths AS upstream, downstream_paths AS downstream
        """
        with self.driver.session() as session:
            rec = session.run(
                cypher,
                repo_id=repo_id,
                file_path=file_path,
            ).single()
            upstream = sorted([p for p in (rec["upstream"] or []) if p])
            downstream = sorted([p for p in (rec["downstream"] or []) if p])
            return upstream, downstream

    def file_dependencies_v2(self, file_path: str, repo_id: str) -> Dict[str, List[str]]:
        """Get files that interact with the given file via cross-file references."""
        down_stream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path})
                -[:DEPENDS_ON]->(ident:ASTNode)
        RETURN ident.file_path
        """

        up_stream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id})
                -[:DEPENDS_ON]->(ident:ASTNode {file_path: $file_path})
        RETURN ref.file_path
        """

        with self.driver.session() as session:
            downstream_files = [record["ident.file_path"] for record in session.run(down_stream_query, repo_id=repo_id, file_path=file_path)]
            upstream_files = [record["ref.file_path"] for record in session.run(up_stream_query, repo_id=repo_id, file_path=file_path)]

        return upstream_files, downstream_files

    def _fetch_def_nodes(self, repo_id: str) -> List[Dict[str, Any]]:
        """
        Bring back enough coordinates to reconstruct the source snippet.
        """
        cypher = """
        MATCH (def:ASTNode)
        WHERE def.is_definition = true
          AND def.node_type IN $types
          AND def.node_id STARTS WITH $repo_id_prefix
        RETURN def.node_id AS node_id,
               def.node_type AS node_type,
               def.file_path AS file_path,
               def.start_line AS start_line,
               def.start_column AS start_column,
               def.end_line AS end_line,
               def.end_column AS end_column
        """
        with self.driver.session() as session:
            res = session.run(
                cypher,
                types=list(DEF_NODE_TYPES),
                repo_id_prefix=repo_id + ":",
            )
            return [r.data() for r in res]

    def _fetch_symbol_from_ast(self, def_node_id: str) -> str:
        """
        For Python trees, the function/class name appears as an `identifier` leaf
        under the definition node. Grab the earliest identifier by traversal order.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH path = (def)-[rels:CONTAINS*..4]->(leaf:ASTNode)
        WHERE leaf.node_type = 'identifier'
        WITH leaf, reduce(s=0, r IN rels | s + coalesce(r.sequence, 1)) AS order_key
        RETURN leaf.name AS ident
        ORDER BY order_key ASC
        LIMIT 1
        """
        with self.driver.session() as session:
            res = session.run(cypher, def_node_id=def_node_id)
            rec = res.single()
            return (rec and (rec["ident"] or "").strip()) or ""

    def _neighbors_up(self, def_node_id: str, def_file: str) -> List[str]:
        """
        Definitions (functions/classes) OUTSIDE this definition that reference/call
        something inside this definition (incoming REFERENCES).
        Returns distinct definition node_ids.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH (def)-[:CONTAINS*]->(def_leaf:ASTNode)
        MATCH (ref_leaf:ASTNode)-[:REFERENCES]->(def_leaf)
        MATCH (ref_def:ASTNode)-[:CONTAINS*]->(ref_leaf)
        WHERE ref_def.is_definition = true
        AND ref_def.node_type IN $types
        AND ref_def.node_id <> $def_node_id
        AND NOT (def)-[:CONTAINS*]->(ref_def)   // exclude nested-in-self
        RETURN DISTINCT ref_def.node_id AS dep_id
        """
        with self.driver.session() as session:
            res = session.run(
                cypher,
                def_node_id=def_node_id,
                types=list(DEF_NODE_TYPES),
            )
            return sorted([r["dep_id"] for r in res])

    def _neighbors_down(self, def_node_id: str, def_file: str) -> List[str]:
        """
        Definitions (functions/classes) OUTSIDE this definition that are referenced/called
        by something inside this definition (outgoing REFERENCES).
        Returns distinct definition node_ids.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH (def)-[:CONTAINS*]->(my_leaf:ASTNode)
        MATCH (my_leaf)-[:REFERENCES]->(tgt_leaf:ASTNode)
        MATCH (tgt_def:ASTNode)-[:CONTAINS*]->(tgt_leaf)
        WHERE tgt_def.is_definition = true
        AND tgt_def.node_type IN $types
        AND tgt_def.node_id <> $def_node_id
        AND NOT (def)-[:CONTAINS*]->(tgt_def)   // exclude nested-in-self
        RETURN DISTINCT tgt_def.node_id AS dep_id
        """
        with self.driver.session() as session:
            res = session.run(
                cypher,
                def_node_id=def_node_id,
                types=list(DEF_NODE_TYPES),
            )
            return sorted([r["dep_id"] for r in res])

    def _fetch_all_def_dependencies(self, repo_id: str) -> Dict[str, Dict[str, set]]:
        """
        For every definition in the repo, compute:
          - downstream: defs this def references (calls/uses)
          - upstream:   defs that reference this def (callers)
        Returns a map: def_id -> {"upstream": set(ids), "downstream": set(ids)}
        """
        cypher = """
        MATCH (d:ASTNode)
        WHERE d.is_definition = true
          AND d.node_type IN $types
          AND d.node_id STARTS WITH $repo_id_prefix

        // downstream: d -> callee_def
        CALL {
          WITH d
          OPTIONAL MATCH (d)-[:CONTAINS*]->(my_leaf:ASTNode)-[:REFERENCES]->(tgt_leaf:ASTNode)
          OPTIONAL MATCH (callee_def:ASTNode)-[:CONTAINS*]->(tgt_leaf)
          WHERE callee_def.is_definition = true
            AND callee_def.node_type IN $types
            AND callee_def.node_id <> d.node_id
          RETURN collect(DISTINCT callee_def.node_id) AS downstream
        }

        // upstream: caller_def -> d
        CALL {
          WITH d
          OPTIONAL MATCH (caller_def:ASTNode)-[:CONTAINS*]->(ref_leaf:ASTNode)-[:REFERENCES]->(my_leaf:ASTNode)
          OPTIONAL MATCH (d)-[:CONTAINS*]->(my_leaf)
          WHERE caller_def.is_definition = true
            AND caller_def.node_type IN $types
            AND caller_def.node_id <> d.node_id
          RETURN collect(DISTINCT caller_def.node_id) AS upstream
        }

        RETURN d.node_id AS def_id, upstream, downstream
        """
        out: Dict[str, Dict[str, set]] = {}
        with self.driver.session() as session:
            res = session.run(
                cypher,
                types=list(DEF_NODE_TYPES),
                repo_id_prefix=repo_id + ":",
            )
            for r in res:
                def_id = r["def_id"]
                upstream = set((r.get("upstream") or []))
                downstream = set((r.get("downstream") or []))
                out[def_id] = {"upstream": upstream, "downstream": downstream}
        return out

    def cross_file_interactions_in_file(self, file_path: str, repo_id: str):
        """Infer cross-file interactions for a given file by finding references to and from definitions in other files."""

        # Downstream: file_path → other files
        downstream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path, is_reference: true})
        -[:REFERENCES]->(ident:ASTNode)
        WHERE ident.file_path <> $file_path
        MATCH (def:ASTNode)
        WHERE def.node_id = ident.parent_id
        RETURN DISTINCT ref.name AS ref_name, def.node_type AS node_type, def.file_path AS def_file_path
        """

        # Upstream: other files → file_path
        upstream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, is_reference: true})
        -[:REFERENCES]->(ident:ASTNode {file_path: $file_path})
        MATCH (def:ASTNode)
        WHERE def.node_id = ident.parent_id
        RETURN DISTINCT ref.file_path AS ref_file_path, ref.name AS ref_name, def.node_type AS node_type
        """

        with self.driver.session() as session:
            # Downstream
            downstream_result = list(session.run(downstream_query, repo_id=repo_id, file_path=file_path))
            # downstream_interactions = [
            #     f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
            #     for record in downstream_result
            # ]
            downstream_interactions = {}
            for record in downstream_result:
                def_file = record['def_file_path']
                interaction = f"{record['ref_name']} REFERENCES {record['node_type']} IN {def_file}"
                if def_file not in downstream_interactions:
                    downstream_interactions[def_file] = []
                downstream_interactions[def_file].append(interaction)

            downstream_files = {
                record['def_file_path'] for record in downstream_result if record['def_file_path'] != file_path
            }

            # Upstream
            upstream_result = list(session.run(upstream_query, repo_id=repo_id, file_path=file_path))
            # upstream_interactions = [
            #     f"{record['ref_name']} IN {record['ref_file_path']} REFERENCES {record['node_type']} IN {file_path}"
            #     for record in upstream_result
            # ]
            upstream_interactions = {}
            for record in upstream_result:
                ref_file = record['ref_file_path']
                interaction = f"{record['ref_name']} IN {ref_file} REFERENCES {record['node_type']} IN {file_path}"
                if ref_file not in upstream_interactions:
                    upstream_interactions[ref_file] = []
                upstream_interactions[ref_file].append(interaction)


            upstream_files = {
                record['ref_file_path'] for record in upstream_result if record['ref_file_path'] != file_path
            }

            return {
                "downstream": {
                    "interactions": downstream_interactions,
                    "files": downstream_files,
                },
                "upstream": {
                    "interactions": upstream_interactions,
                    "files": upstream_files,
                },
            }

    def close(self):
        """Close the Neo4j connection."""
        if self.driver:
            self.driver.close()
            logger.info("Neo4j connection closed")


class MyMongoClient:
    def __init__(self, *args, **kwargs):
        self._client = MongoClient(*args, **kwargs)

    def get_database(self, name: str):
        return self._client[name]
    
def get_entry_point_files(db_client, repo_id: str) -> List[str]:
    collection = db_client["mental_model"]
    doc = collection.find_one({"repo_id": repo_id, "document_type": "REPO_ENTRY_POINTS"})
    if doc:
        absolute_paths = doc.get("entry_points", [])
        
        relative_paths = []
        for abs_path in absolute_paths:
            # Check and remove the repo_id prefix from the absolute path
            if abs_path.startswith(repo_id):
                rel_path = abs_path[len(repo_id):]
                if rel_path.startswith("/") or rel_path.startswith("\\"):
                    rel_path = rel_path[1:]
                relative_paths.append(rel_path)
            else:
                relative_paths.append(abs_path)  # Fallback to absolute path if prefix not found
        
        return relative_paths
    return []
    # entry_point_files = []
    # for doc in docs:
    #     entry_point_files.append(doc.get("file_path"))
    # return entry_point_files

def get_potential_entry_points(db_client, repo_id: str) -> List[str]:
    collection = db_client["mental_model"]
    docs = collection.find({"repo_id": repo_id, "document_type": "POTENTIAL_ENTRY_POINTS"})
    entry_point_files = []
    
    # Each docs contains the `file_path` field
    for doc in docs:
        entry_point_files.append(doc.get("file_path"))

    return entry_point_files

def get_repo_summary(db_client, repo_id: str) -> str:
    collection = db_client["mental_model"]
    doc = collection.find_one({"repo_id": repo_id, "document_type": "REPO_SUMMARY"})
    if doc:
        return doc.get("data", "")  
    return ""

def get_brief_file_overviews(db_client, repo_id: str, file_paths: List[str]) -> List[Dict[str, str]]:
        """
        Fetch BRIEF_FILE_OVERVIEW for a batch of files.
        Returns list of {file_path, brief} dicts.
        """
        collection = db_client["mental_model"]
        result = []
        for file_path in file_paths:
            doc = collection.find_one(
                {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW", "file_path": file_path},
                {"_id": 0, "data": 1},
            )
            brief = (doc or {}).get("data", "")
            if brief:
                result.append({"file_path": file_path, "brief": brief})
        return result

def get_critical_file_paths(db_client, repo_id: str) -> List[str]:
    collection = db_client["mental_model"]
    
    #Get all file_path in collection that have "document_type": "BRIEF_FILE_OVERVIEW"
    result = collection.find({"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"})
    critical_files = [doc.get("file_path") for doc in result if doc.get("file_path")]
    return critical_files

# Global shared client
neo4j_client: Neo4jClient = None
mongo_client: MyMongoClient = None

def init_neo4j_client():
    global neo4j_client
    neo4j_client = Neo4jClient()

def init_mongo_client():
    global mongo_client
    client = MyMongoClient()
    mongo_client = client.get_database(Config.MONGO_DB_NAME)

def get_neo4j_client() -> Neo4jClient:
    if neo4j_client is None:
        init_neo4j_client()
    return neo4j_client

def get_mongo_client() -> MyMongoClient:
    if mongo_client is None:
        init_mongo_client()
    return mongo_client
