from ..db import get_neo4j_client

def fetch_code_for_def(def_name: str) -> str:
    neo4j_client = get_neo4j_client()
    
    ## Fetch the start_line, start_column, end_line, end_column for the definition from neo4j.
    ## Write neo4j query where repo_id = "dictquery", is_definition = True, and name = def_name
    query = """
    MATCH (n:Node {repo_id: 'dictquery', is_definition: true, name: $def_name})
    RETURN n.start_line AS start_line, n.start_column AS start_column,
           n.end_line AS end_line, n.end_column AS end_column, 
           n.file_path AS file_path, n.node_type as node_type
    """

    
    code_snippet_coords = []

    try:
        with neo4j_client.driver.session() as session:
            result = session.run(query, def_name=def_name)

        if not result:
            return {"error": f"Definition '{def_name}' not found in the repository."}

        for record in result:
            code_snippet_coords.append({
                "start_line": record['start_line'],
                "start_column": record['start_column'],
                "end_line": record['end_line'],
                "end_column": record['end_column'],
                "file_path": record['file_path'],
                "node_type": record['node_type']
            })

    except Exception as e:
        return {"error": str(e)}

    if not code_snippet_coords:
        return {"error": f"Definition '{def_name}' not found in the repository."}
    
    code_snippets = []

    # Fetch code snippets for each set of coordinates
    for coords in code_snippet_coords:
        start_line = coords['start_line']
        start_column = coords['start_column']
        end_line = coords['end_line']
        end_column = coords['end_column']
        file_path = coords['file_path']

        base_path = "data"
        # Now fetch the code snippet from the file using the extracted coordinates
        with open(base_path / file_path, 'r') as f:
            lines = f.readlines()
            code_snippet = ''.join(lines[start_line:end_line])
            code_snippets.append({"code": code_snippet, "file_path": file_path})

    return {"code_snippets": code_snippets}
