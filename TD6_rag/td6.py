import re
from typing import List, Tuple
from rdflib import Graph
import requests

# Config
TTL_FILE = "expanded_kb_cleaned.ttl"
OLLAMA_URL = "http://localhost:11434/api/generate"
GEMMA_MODEL = "mistral" 

MAX_PREDICATES = 80
MAX_CLASSES = 40
SAMPLE_TRIPLES = 20

# Call local LLM
def ask_local_llm(prompt: str, model: str = GEMMA_MODEL) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False 
    }
    response = requests.post(OLLAMA_URL, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"Ollama API error {response.status_code}: {response.text}")
    return response.json().get("response", "")

# Load RDF graph
def load_graph(ttl_path: str) -> Graph:
    g = Graph()
    g.parse(ttl_path, format="turtle")
    print(f"Loaded {len(g)} triples from {ttl_path}")
    return g

# Build a schema summary

def get_prefix_block(g: Graph) -> str:
    defaults = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "owl": "http://www.w3.org/2002/07/owl#",
    }
    ns_map = {p: str(ns) for p, ns in g.namespace_manager.namespaces()}
    for k, v in defaults.items():
        ns_map.setdefault(k, v)
    lines = [f"PREFIX {p}: <{ns}>" for p, ns in ns_map.items()]
    return "\n".join(sorted(lines))

def list_distinct_predicates(g: Graph, limit=MAX_PREDICATES) -> List[str]:
    q = f"SELECT DISTINCT ?p WHERE {{ ?s ?p ?o . }} LIMIT {limit}"
    return [str(row.p) for row in g.query(q)]

def list_distinct_classes(g: Graph, limit=MAX_CLASSES) -> List[str]:
    q = f"SELECT DISTINCT ?cls WHERE {{ ?s a ?cls . }} LIMIT {limit}"
    return [str(row.cls) for row in g.query(q)]

def sample_triples(g: Graph, limit=SAMPLE_TRIPLES) -> List[Tuple[str, str, str]]:
    q = f"SELECT ?s ?p ?o WHERE {{ ?s ?p ?o . }} LIMIT {limit}"
    return [(str(r.s), str(r.p), str(r.o)) for r in g.query(q)]

def list_property_labels(g: Graph) -> List[str]:
    q = """
    SELECT DISTINCT ?p ?label WHERE { 
      ?p rdfs:label ?label .
      FILTER(STRSTARTS(STR(?p), "http://www.wikidata.org/prop/direct/"))
    } 
    """
    return [f"{str(row.p)} means '{str(row.label)}'" for row in g.query(q)]

def build_schema_summary(g: Graph) -> str:
    prefixes = get_prefix_block(g)
    preds = list_distinct_predicates(g)
    clss = list_distinct_classes(g)
    samples = sample_triples(g)
    prop_labels = list_property_labels(g) # New line
    
    pred_lines = "\n".join(f"- {p}" for p in preds)
    cls_lines = "\n".join(f"- {c}" for c in clss)
    sample_lines = "\n".join(f"- {s} {p} {o}" for s, p, o in samples)
    prop_lines = "\n".join(f"- {l}" for l in prop_labels) # New line
    
    return f"""
{prefixes}

# Property Meanings (CRITICAL)
{prop_lines}

# Predicates (sampled, unique up to {MAX_PREDICATES})
{pred_lines}

# Classes / rdf:type (sampled, unique up to {MAX_CLASSES})
{cls_lines}

# Sample triples (up to {SAMPLE_TRIPLES})
{sample_lines}
""".strip()

# Prompting for SPARQL generation
SPARQL_INSTRUCTIONS = """
You are an expert SPARQL generator. Convert the user QUESTION into a valid SPARQL 1.1 SELECT query.
Follow these rules strictly:
- Do NOT output any natural language, greetings, or explanations.
- Return ONLY the query wrapped in ```sparql ... ```
- Use the provided SCHEMA SUMMARY to find the correct prefixes and predicates.
- Use the prefix `PREFIX ns1: <wdt:>` instead of the standard `wdt:` URI.
- Query properties using `ns1:` (e.g., `ns1:P57` for director).
- Never append `@en` to string literals. Always use plain strings (e.g., "Inception", not "Inception"@en).
- Never hardcode Wikidata entity IDs (like wd:Q123) in the query. Always use variables (like ?entity) and match them to the user's requested entity using rdfs:label "Entity Name".

Example Output:
```sparql
PREFIX ns1: <wdt:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?director WHERE {
  ?movie rdfs:label "Inception" .
  ?movie ns1:P57 ?director .
}
```
"""

def make_sparql_prompt(schema_summary: str, question: str) -> str:
    return f"{SPARQL_INSTRUCTIONS}\n\nSCHEMA SUMMARY:\n{schema_summary}\n\nQUESTION:\n{question}\n\nReturn only the SPARQL query in a code block."

CODE_BLOCK_RE = re.compile(r"```(?:sparql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

def extract_sparql_from_text(text: str) -> str:
    m = CODE_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # If no code block is found, strip out common conversational prefixes
    lines = text.split('\n')
    cleaned_lines = [line for line in lines if not line.lower().startswith(("here", "sure", "this", "the"))]
    return "\n".join(cleaned_lines).strip()

def generate_sparql(question: str, schema_summary: str) -> str:
    raw = ask_local_llm(make_sparql_prompt(schema_summary, question))
    return extract_sparql_from_text(raw)

# Execute SPARQL and handle errors
def run_sparql(g: Graph, query: str) -> Tuple[List[str], List[Tuple]]:
    res = g.query(query)
    vars_ = [str(v) for v in res.vars]
    rows = [tuple(str(cell) for cell in r) for r in res]
    return vars_, rows

REPAIR_INSTRUCTIONS = """
The previous SPARQL failed to execute. Using the SCHEMA SUMMARY and the ERROR MESSAGE, 
return a corrected SPARQL 1.1 SELECT query. Follow strictly: 
- Use only known prefixes/IRIs. 
- Keep it as simple and robust as possible. 
- Return only a single code block with the corrected SPARQL.
"""

def repair_sparql(schema_summary: str, question: str, bad_query: str, error_msg: str) -> str:
    prompt = f"{REPAIR_INSTRUCTIONS}\n\nSCHEMA SUMMARY:\n{schema_summary}\n\nORIGINAL QUESTION:\n{question}\n\nBAD SPARQL:\n{bad_query}\n\nERROR MESSAGE:\n{error_msg}\n\nReturn only the corrected SPARQL in a code block."
    raw = ask_local_llm(prompt)
    return extract_sparql_from_text(raw)

# Orchestrating the full SPARQL generation + execution with optional repair
def answer_with_sparql_generation(g: Graph, schema_summary: str, question: str, try_repair: bool = True) -> dict:
    sparql = generate_sparql(question, schema_summary)
    
    try:
        vars_, rows = run_sparql(g, sparql)
        return {"query": sparql, "vars": vars_, "rows": rows, "repaired": False, "error": None}
    except Exception as e:
        err = str(e)
        if try_repair:
            repaired = repair_sparql(schema_summary, question, sparql, err)
            try:
                vars_, rows = run_sparql(g, repaired)
                return {"query": repaired, "vars": vars_, "rows": rows, "repaired": True, "error": None}
            except Exception as e2:
                return {"query": repaired, "vars": [], "rows": [], "repaired": True, "error": str(e2)}
        return {"query": sparql, "vars": [], "rows": [], "repaired": False, "error": err}

# Baseline: answer without RAG (just ask the LLM directly)
def answer_no_rag(question: str) -> str:
    return ask_local_llm(f"Answer the following question as best as you can:\n\n{question}")

# CLI demo
def pretty_print_result(result: dict):
    if result.get("error"):
        print("\n[Execution Error]", result["error"])
    print("\n[SPARQL Query Used]\n" + result["query"])
    print("\n[Repaired?]", result["repaired"])
    
    vars_ = result.get("vars", [])
    rows = result.get("rows", [])
    
    if not rows:
        print("\n[No rows returned]")
        return
        
    print("\n[Results]")
    print(" | ".join(vars_))
    for r in rows[:20]:
        print(" | ".join(r))
    if len(rows) > 20:
        print(f"... (showing 20 of {len(rows)})")

if __name__ == "__main__":
    g = load_graph(TTL_FILE)
    schema = build_schema_summary(g)
    
    while True:
        q = input("\nQuestion (or 'quit'): ").strip()
        if q.lower() == "quit": break
            
        print("\n--- Baseline (No RAG) ---")
        print(answer_no_rag(q))
        
        print("\n--- SPARQL-generation RAG (Mistral + rdflib) ---")
        pretty_print_result(answer_with_sparql_generation(g, schema, q, try_repair=True))
