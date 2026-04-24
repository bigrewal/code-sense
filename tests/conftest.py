"""
Shared pytest fixtures for database tests.
"""

import pytest
from unittest.mock import Mock, MagicMock


@pytest.fixture(scope="session")
def mock_config():
    """
    Mock configuration for tests.

    Returns a mock Config object with test-safe values.
    """
    config = Mock()
    config.NEO4J_URI = "bolt://localhost:7688"
    config.NEO4J_USER = "neo4j"
    config.NEO4J_PASSWORD = "testpassword"
    config.NEO4J_MAX_POOL_SIZE = 50
    config.NEO4J_MAX_CONNECTION_LIFETIME = 3600
    config.NEO4J_CONNECTION_TIMEOUT = 30
    config.NEO4J_BATCH_SIZE = 1000

    config.SQLITE_DB_PATH = ":memory:"
    config.LOG_DB_QUERIES = False

    return config


@pytest.fixture(autouse=True)
def reset_singletons():
    """
    Reset singleton instances between tests to ensure test isolation.

    This fixture runs automatically before each test.
    """
    # Import here to avoid circular dependencies
    from app import db

    # Reset Neo4jClient singleton
    if hasattr(db, 'Neo4jClient'):
        db.Neo4jClient._instance = None

    # Reset database singleton
    if hasattr(db, 'SQLiteClient'):
        db.SQLiteClient._instance = None

    yield

    # Cleanup after test
    if hasattr(db, 'Neo4jClient'):
        db.Neo4jClient._instance = None
    if hasattr(db, 'SQLiteClient'):
        db.SQLiteClient._instance = None
