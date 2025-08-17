from typing import List

from ..db import get_neo4j_client

def lookup_refs_for_def(def_name: str) -> List[str]:
    neo4j_client = get_neo4j_client()
    repo_id: str = "dictquery"
    query = """
        MATCH (def:ASTNode {name: $def_name, repo_id: $repo_id, is_definition: true})
        MATCH (def)-[:CONTAINS {sequence:1}]->(ident:ASTNode)
        MATCH (ident)-[:REFERENCES]->(ref:ASTNode)
        MATCH (ref_parent:ASTNode {node_id: ref.parent_id})
        RETURN DISTINCT ref_parent.name AS def_name, ref_parent.node_type AS node_type
    """
    with neo4j_client.driver.session() as session:
        result = session.run(query, def_name=def_name, repo_id=repo_id)
        refs = [
            f"{record['def_name']} ({record['node_type']})"
            for record in result
        ]
        return refs