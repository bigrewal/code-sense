from typing import List

from ..db import get_neo4j_client

def lookup_def_usages(def_name: str, repo_id: str = "data/xai-sdk-python") -> List[str]:
    # Stub: Return list of usages
    # return ["used_in_function_a"]

    neo4j_client = get_neo4j_client()
    query = """
        MATCH (def:ASTNode {name: $def_name, repo_id: $repo_id, is_definition: true})
        MATCH (def)-[:CONTAINS*]->(ref:ASTNode {is_reference: true})
        MATCH (ref)-[:REFERENCES]->(ident:ASTNode)
        MATCH (def_external:ASTNode {node_id: ident.parent_id})
        WHERE def_external <> def AND NOT EXISTS((def)-[:CONTAINS*]->(def_external))
        RETURN DISTINCT def_external.name AS def_name, def_external.node_type AS node_type, def_external.file_path AS file_path
    """
    with neo4j_client.driver.session() as session:
        result = session.run(query, def_name=def_name, repo_id=repo_id)
        usages = [
            f"{record['def_name']} ({record['node_type']}) in {record['file_path']}"
            for record in result
        ]
        return "\n".join(usages)