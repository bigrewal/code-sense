#### Purge Neo4j DB
MATCH (n)-[r]-() DELETE r
MATCH (n) DELETE n

CALL {
  MATCH ()-[r]-()
  RETURN r
  LIMIT 100000
}
DELETE r;

CALL {
  MATCH (r)
  RETURN r
  LIMIT 500000
}
DELETE r;