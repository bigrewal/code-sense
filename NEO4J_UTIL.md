#### Purge Neo4j DB
MATCH (n)-[r]-() DELETE r
MATCH (n) DELETE n

CALL apoc.periodic.commit(
  "MATCH (n:ASTNode)
   WHERE n.repo_name = 'astropy'
   WITH n LIMIT 10000
   DETACH DELETE n
   RETURN count(n)"
);


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