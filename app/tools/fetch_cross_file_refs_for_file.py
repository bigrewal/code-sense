from typing import List

from ..db import get_neo4j_client

def fetch_cross_file_refs_for_file(file_path: str) -> List[str]:
    """Infer cross-file interactions for a given file by finding references to definitions in other files."""

    neo4j_client = get_neo4j_client()
    repo_id: str = "dictquery"
    query = """
    MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path, is_reference: true})
    -[:REFERENCES]->(ident:ASTNode)
    WHERE ident.file_path <> $file_path
    MATCH (def:ASTNode)
    WHERE def.node_id = ident.parent_id
    RETURN DISTINCT ref.name AS ref_name, def.node_type AS node_type, def.file_path AS def_file_path
    """
    with neo4j_client.driver.session() as session:
        result = session.run(query, repo_id=repo_id, file_path=file_path)
        interactions = [
            f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
            for record in result
        ]

        return {"refs": interactions}