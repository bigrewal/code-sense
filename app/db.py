import logging
from .attention_integration import AttentionDBRuntime
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
    
    def _batch_create_nodes(self, session, nodes: List[ASTNode], repo_id: str):
        """Create AST nodes in batches."""
        total_batches = (len(nodes) + self.batch_size - 1) // self.batch_size
        
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
        
        logger.info(f"Successfully created {len(nodes)} nodes in {total_batches} batches")
    
    def _batch_create_edges(self, session, edges: List[Dict[str, Any]], repo_id: str):
        """Create edges in batches."""
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
            
            logger.debug(f"Processed edge batch {batch_num}/{total_batches}")
        
        logger.info(f"Successfully created {len(edges)} edges in {total_batches} batches")
    
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
    docs = collection.find({"repo_id": repo_id, "document_type": "POTENTIAL_ENTRY_POINTS"})
    entry_point_files = []
    for doc in docs:
        entry_point_files.append(doc.get("file_path"))
    return entry_point_files

def get_repo_summary(db_client, repo_id: str) -> str:
    collection = db_client["mental_model"]
    doc = collection.find_one({"repo_id": repo_id, "document_type": "REPO_SUMMARY"})
    if doc:
        return doc.get("data", "")
    return ""

attention_db_runtime = AttentionDBRuntime(data_root="data")

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
