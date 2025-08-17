from typing import List

from ..db import get_neo4j_client

def fetch_entry_point_files() -> List[str]:
    # Stub
    # return ["main.py", "app.py"]
    neo4j_client = get_neo4j_client()
    repo_id: str = "dictquery"
    
    query = """
        MATCH (n:ASTNode)
        WHERE n.repo_id = $repo_id
        WITH n.file_path AS file_path, COLLECT(n) AS nodes
        WHERE NONE(node IN nodes WHERE SIZE([(other:ASTNode)-[:REFERENCES]->(node) WHERE other.file_path <> file_path | other]) > 0)
        RETURN DISTINCT file_path
    """
    with neo4j_client.driver.session() as session:
        result = session.run(query, repo_id=repo_id)
        file_paths = [record['file_path'] for record in result]
        return {"file_paths": file_paths}