import os
import traceback
from typing import Dict, List
from pathlib import Path

from .db import get_mongo_client, get_neo4j_client

class MentalModelFetcher:
    def __init__(self, model_path: str = "data/fastapi/mental_model/mental_model.md"):
        self.model_path = model_path
        self.mongo_client = get_mongo_client()
        self.mental_model_collection = self.mongo_client["mental_model"]

    def fetch(self) -> Dict[str, any]:
        """Load and parse the mental model Markdown file into a structured dict."""
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"Mental model file not found: {self.model_path}")

        with open(self.model_path, "r") as f:
            content = f.read()

        return {"overview": content}


    def seed_prompt(self, repo_id: str) -> str:
        """Seed the prompt with the mental model data in a formatted markdown."""
        # 1. Get all file_paths for document_type=ENTRY_POINTS
        try:
            entry_points_cursor = self.mental_model_collection.find({
                "repo_id": repo_id,
                "document_type": "POTENTIAL_ENTRY_POINTS"
            })
            entry_points = list(entry_points_cursor)
            entry_point_files = {ep['file_path'] for ep in entry_points}

            interactions = self.get_cross_file_interactions(entry_point_files, repo_id)

            # 3. Get the BRIEF_FILE_OVERVIEW for entry point files
            brief_overviews_cursor = self.mental_model_collection.find({
                "repo_id": repo_id,
                "document_type": "BRIEF_FILE_OVERVIEW",
                "file_path": {"$in": list(entry_point_files)}
            })
            brief_overviews = list(brief_overviews_cursor)
            # 4. Create a nicely formatted markdown for the LLM
            markdown = f"# Overview of {repo_id}\n\n"

            markdown += "## List of Potential Entry Point Files\n"
            if entry_point_files:
                for ep in entry_point_files:
                    markdown += f"- {ep}\n"
            else:
                markdown += "No entry points found.\n"

            markdown += "\n## Brief Overviews of each potential entry point file\n"
            if brief_overviews:
                for bo in brief_overviews:
                    markdown += f"### {bo['file_path']}\n{bo['data']}\n"
            else:
                markdown += "No brief overviews found for entry points.\n"

            markdown += "\n## Cross-File Interactions for each entry point file\n"
            if interactions:
                for interaction in interactions:
                    markdown += f"### {interaction['file_path']}\n"
                    markdown += f"```\n{interaction['data']}\n```\n\n"
            else:
                markdown += "No cross file interactions found for entry points.\n"

            # Write the markdown to file and save it to ../{repo_id}
            # output_dir = Path(f"../{repo_id}")
            # output_dir.mkdir(parents=True, exist_ok=True)

            # with open(output_dir / "prompt_seed.md", "w") as f:
            #     f.write(markdown)
            
            # print(f"Saved mental model prompt seed to {output_dir / 'prompt_seed.md'}")
            return markdown
        except Exception as e:
            traceback.print_exc()
            print(f"Error seeding prompt for repo_id: {repo_id}, error: {e}")
            return ""


    def get_cross_file_interactions(self, entry_point_files: list, repo_id: str) -> List[dict]:
        """Infer cross-file interactions for a given file by finding references to definitions in other files."""

        mongo_client = get_mongo_client()
        mental_model_collection = mongo_client["mental_model"]

        # Get list of all file_paths that have BRIEF_FILE_OVERVIEW
        brief_file_overviews = mental_model_collection.find({
            "repo_id": repo_id,
            "document_type": "BRIEF_FILE_OVERVIEW"
        })
        brief_file_paths = {doc["file_path"] for doc in brief_file_overviews}

        interactions_res = []

        for file_path in entry_point_files:
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

                # Create a \n separated string of interactions if record['def_file_path'] is also in brief_file_paths
                interactions = [
                    f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
                    for record in records if record['def_file_path'] in brief_file_paths
                ]

                interactions_res.append({"file_path": file_path,
                                           "data": "\n".join(interactions)})

        return interactions_res