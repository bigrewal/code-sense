import os
from typing import Any, Dict
from dotenv import load_dotenv

load_dotenv(".env.local")

class Config:
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    GPT_OSS_20GB_MODEL = "openai/gpt-oss-20b"
    GPT_OSS_120GB_MODEL = "openai/gpt-oss-120b"
    GROK_4_NON_REASONING_MODEL = "grok-4-1-fast-non-reasoning"
    GROK_4_REASONING_MODEL = "grok-4-1-fast-reasoning"
    LLM_TEMPERATURE = 0.7
    LLM_MAX_TOKENS = 10240

    # AWS S3 Configuration
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "eu-west-2"
    S3_BUCKET_NAME: str = "code-analysis-repos"

    # New Neo4j configs
    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "llamaindex")

    # MongoDB Configuration
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB_NAME: str = "code_comprehension"
    CHUNK_SUMMARIES_COLLECTION: str = "chunk_summaries"
    JOB_STATUS_COLLECTION: str = "job_status"


    # Pipeline Stage Configurations
    @property
    def UPLOAD_CONFIG(self) -> Dict[str, Any]:
        return {
            "max_file_size": 100 * 1024 * 1024,  # 100MB
            "allowed_extensions": [".zip", ".tar.gz", ".tar"]
        }
    
    @property
    def S3_CONFIG(self) -> Dict[str, Any]:
        return {
            "bucket_name": self.S3_BUCKET_NAME,
            "aws_config": {
                "aws_access_key_id": self.AWS_ACCESS_KEY_ID,
                "aws_secret_access_key": self.AWS_SECRET_ACCESS_KEY,
                "region_name": self.AWS_REGION
            }
        }
    
    @property
    def RESOLVER_CONFIG(self) -> Dict[str, Any]:
        return {
            "include_unresolved": True,
            "reuse_database": False
        }
    
    @property
    def AST_CONFIG(self) -> Dict[str, Any]:
        return {
            "supported_languages": ["python", "javascript", "typescript"],
            "max_file_size": 1024 * 1024,  # 1MB per file
        }
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @classmethod
    def validate(cls):
        if not cls.GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY must be set in .env")
        if not cls.NEO4J_URI:
            raise ValueError("NEO4J_URI must be set in .env")
        if not cls.NEO4J_USER:
            raise ValueError("NEO4J_USER must be set in .env")
        if not cls.NEO4J_PASSWORD:
            raise ValueError("NEO4J_PASSWORD must be set in .env")