CREATE CONSTRAINT concept_id IF NOT EXISTS
FOR (n:Concept) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT skill_id IF NOT EXISTS
FOR (n:Skill) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT exercise_id IF NOT EXISTS
FOR (n:Exercise) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT book_id IF NOT EXISTS
FOR (n:Book) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT chapter_id IF NOT EXISTS
FOR (n:Chapter) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT section_id IF NOT EXISTS
FOR (n:Section) REQUIRE n.id IS UNIQUE;

CREATE INDEX concept_name IF NOT EXISTS
FOR (n:Concept) ON (n.name);

CREATE INDEX skill_name IF NOT EXISTS
FOR (n:Skill) ON (n.name);

CREATE INDEX exercise_name IF NOT EXISTS
FOR (n:Exercise) ON (n.name);

CREATE INDEX concept_scope IF NOT EXISTS
FOR (n:Concept) ON (n.subject, n.stage, n.grade, n.semester, n.edition);

CREATE INDEX skill_scope IF NOT EXISTS
FOR (n:Skill) ON (n.subject, n.stage, n.grade, n.semester, n.edition);

CREATE INDEX exercise_scope IF NOT EXISTS
FOR (n:Exercise) ON (n.subject, n.stage, n.grade, n.semester, n.edition);

CREATE FULLTEXT INDEX concept_fulltext IF NOT EXISTS
FOR (n:Concept) ON EACH [n.name, n.search_text]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};

CREATE FULLTEXT INDEX skill_fulltext IF NOT EXISTS
FOR (n:Skill) ON EACH [n.name, n.search_text]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};

CREATE FULLTEXT INDEX exercise_fulltext IF NOT EXISTS
FOR (n:Exercise) ON EACH [n.name, n.search_text]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};

CREATE VECTOR INDEX concept_vector IF NOT EXISTS
FOR (n:Concept) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 512,
  `vector.similarity_function`: 'cosine'
}};

CREATE VECTOR INDEX skill_vector IF NOT EXISTS
FOR (n:Skill) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 512,
  `vector.similarity_function`: 'cosine'
}};

CREATE VECTOR INDEX exercise_vector IF NOT EXISTS
FOR (n:Exercise) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 512,
  `vector.similarity_function`: 'cosine'
}};
