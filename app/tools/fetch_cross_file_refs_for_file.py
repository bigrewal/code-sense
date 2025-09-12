from typing import List

from ..db import get_neo4j_client, get_mongo_client

def fetch_cross_file_refs_for_file(file_path: str, repo_id: str = "data/dictquery") -> str:
    """Fetch cross-file interactions for a given file, distinguishing between important files (with brief summaries) and others.
    
    Returns a formatted string for LLM, listing references to definitions in other files.
    """
    # Fetch important files from MongoDB
    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]
    brief_file_overviews = mental_model_collection.find({
        "repo_id": repo_id,
        "document_type": "BRIEF_FILE_OVERVIEW"
    })
    brief_file_paths = {doc["file_path"] for doc in brief_file_overviews}

    # Query Neo4j for cross-file references
    neo4j_client = get_neo4j_client()
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
        records = list(result)

        # Separate interactions into important and non-important files
        important_interactions = [
            f"- {record['ref_name']} references {record['node_type']} in {record['def_file_path']}"
            for record in records if record['def_file_path'] in brief_file_paths
        ]
        non_important_interactions = [
            f"- {record['ref_name']} references {record['node_type']} in {record['def_file_path']}"
            for record in records if record['def_file_path'] not in brief_file_paths
        ]

        # Construct LLM-friendly output
        output = []
        if important_interactions:
            output.append("### Interactions with Important Files (with Brief Summaries)")
            output.extend(important_interactions)
        else:
            output.append("### Interactions with Important Files\nNone found.")

        if non_important_interactions:
            output.append("\n### Dependencies on Other Files (No Brief Summaries)")
            output.extend(non_important_interactions)
        else:
            output.append("\n### Dependencies on Other Files\nNone found.")

        return "\n".join(output)
    

def fetch_cross_file_refs_for_file_as_list(file_path: str, repo_id: str = "data/dictquery") -> str:
    """Fetch cross-file interactions for a given file, distinguishing between important files (with brief summaries) and others.
    
    Returns a formatted string for LLM, listing references to definitions in other files.
    """
    # print(f"Fetching cross-file references for: {file_path} in repo: {repo_id}")
    # Fetch important files from MongoDB
    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]
    brief_file_overviews = mental_model_collection.find({
        "repo_id": repo_id,
        "document_type": "BRIEF_FILE_OVERVIEW"
    })
    brief_file_paths = {doc["file_path"] for doc in brief_file_overviews}

    # Query Neo4j for cross-file references
    neo4j_client = get_neo4j_client()
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
        records = list(result)

        # Separate interactions into important and non-important files
        important_interactions = [
            f"- {record['ref_name']} references {record['node_type']} in {record['def_file_path']}"
            for record in records if record['def_file_path'] in brief_file_paths
        ]
        non_important_interactions = [
            f"- {record['ref_name']} references {record['node_type']} in {record['def_file_path']}"
            for record in records if record['def_file_path'] not in brief_file_paths
        ]

        # Construct LLM-friendly output
        output = []
        if important_interactions:
            output.append("### Interactions with Important Files (with Brief Summaries)")
            output.extend(important_interactions)
        else:
            output.append("### Interactions with Important Files\nNone found.")

        if non_important_interactions:
            output.append("\n### Dependencies on Other Files (No Brief Summaries)")
            output.extend(non_important_interactions)
        else:
            output.append("\n### Dependencies on Other Files\nNone found.")

        llm_output = "\n".join(output)

        # Create a list of record['def_file_path'] that are in brief_file_paths
        important_file_paths = [
            record['def_file_path'] for record in records if record['def_file_path'] in brief_file_paths
        ]

        # print(f"Cross-file references fetched for: {file_path} in repo: {repo_id}")
        return llm_output, important_file_paths