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

    config.MONGO_URI = "mongodb://testuser:testpassword@localhost:27018"
    config.MONGO_DB_NAME = "test_code_comprehension"
    config.MONGO_MAX_POOL_SIZE = 50
    config.MONGO_MIN_POOL_SIZE = 10
    config.MONGO_MAX_IDLE_TIME_MS = 300000
    config.MONGO_SERVER_SELECTION_TIMEOUT_MS = 30000
    config.MONGO_CONNECT_TIMEOUT_MS = 20000
    config.MONGO_SOCKET_TIMEOUT_MS = 300000

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

    # Reset MyMongoClient singleton
    if hasattr(db, 'MyMongoClient'):
        db.MyMongoClient._instance = None

    yield

    # Cleanup after test
    if hasattr(db, 'Neo4jClient'):
        db.Neo4jClient._instance = None
    if hasattr(db, 'MyMongoClient'):
        db.MyMongoClient._instance = None
