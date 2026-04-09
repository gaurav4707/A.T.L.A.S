---
agent: agent
description: ATLAS v2 Phase 1 — ChromaDB Memory + WebSocket Streaming
---

Build Phase 1 of ATLAS v2. Read #file:../copilot-instructions.md.
v1 is working. Do NOT touch: classifier.py, llm_engine.py, validator.py,
security.py, executor.py, verifier.py, rollback.py, pc_control.py, history.py.
Test 'atlas --status' after every file you change. It must keep working.

MODULE 1 — memory.py (NEW FILE)
ChromaDB persistent client stored at settings.get('chroma_path') default '.atlas_chroma'

Setup:
from chromadb import PersistentClient
from sentence_transformers import SentenceTransformer

\_client = PersistentClient(path=chroma_path)
\_facts = \_client.get_or_create_collection("facts")
\_summaries = \_client.get_or_create_collection("summaries")
\_encoder = SentenceTransformer('all-MiniLM-L6-v2') # load once, cache

sliding_window = deque(maxlen=settings.get('session_memory_turns', 8) \* 2)

Sliding window:
add_to_sliding(role: str, content: str) → appends to deque
get_sliding_context() → list[dict] of {"role":role,"content":content}

ChromaDB writes (confidence-gated):
store*fact(fact: str, confidence: float, source: str):
threshold = settings.get('memory_confidence_threshold', 0.75)
if confidence < threshold: return (discard — never write speculative facts)
embedding = \_encoder.encode(fact).tolist()
\_facts.add(documents=[fact], embeddings=[embedding],
ids=[f"fact*{uuid4()}"],
metadatas=[{"source":source,"timestamp":datetime.now().isoformat()}])

ChromaDB reads:
retrieve(query: str, n_results: int = 5) → list[str]:
embedding = \_encoder.encode(query).tolist()
results = \_facts.query(query_embeddings=[embedding], n_results=n_results)
return results['documents'][0] if results['documents'] else []

retrieve_summaries(n_results: int = 3) → list[str]:
(use dummy embedding or latest summaries ordered by metadata timestamp)

Memory expiry:
review*and_expire():
expiry_days = settings.get('memory_expiry_days', 30)
all_facts = \_facts.get(include=['metadatas','ids'])
for id*, meta in zip(all*facts['ids'], all_facts['metadatas']):
age = datetime.now() - datetime.fromisoformat(meta['timestamp'])
if age.days > expiry_days:
\_facts.delete(ids=[id*])
print(f"[dim]I've forgotten a fact — it was over {expiry_days} days old.[/dim]")

Context assembly (token-budgeted):
get_context_for_llm(query: str) → str:
budget = 2200
system = build_system_prompt() # ~400 tokens
facts = retrieve(query) # ~800 tokens
window = get_sliding_context() # ~1000 tokens
assembled = system + format(facts) + format(window)
if token_count(assembled) > budget:
trim facts list first, then oldest window entries
return assembled

Expose all functions above.

Update main.py:
Import memory. Replace session_ctx list with memory calls.
After each exchange: memory.add_to_sliding(role, content)
Pass memory.get_context_for_llm(query) to llm_engine.query()
On startup: memory.review_and_expire()

Update llm_engine.py:
query(prompt: str, context_str: str) → dict (was: session_context: list)
Accept pre-assembled string, prepend to prompt. Keep all other logic identical.

Add to config.json defaults (in settings.py):
"chroma_path": ".atlas_chroma"
"memory_confidence_threshold": 0.75
"memory_expiry_days": 30

MODULE 2 — context_pruner.py (NEW FILE)
Background thread. Runs every 30 minutes. Uses Mistral 7B to compress.
Does NOT write to any JSON file — only writes to ChromaDB.

import threading, time, ollama

stop_event = threading.Event()

def pruner_loop():
while not stop_event.is_set():
stop_event.wait(timeout=1800) # 30 minutes, but wakes on stop signal
if not stop_event.is_set():
compress_and_store()

def compress_and_store():
context = memory.get_sliding_context()
if len(context) < 4: return
messages_text = "\n".join(f"{m['role']}: {m['content']}" for m in context)
prompt = f"Summarise this conversation in 3-5 sentences for future reference:\n{messages_text}"
try:
resp = ollama.generate(model=settings.get('model'), prompt=prompt)
summary = resp['response'].strip()
memory.store_fact(summary, confidence=0.9, source="session_pruner")
except Exception as e:
pass # never crash the background thread

def start_pruner():
t = threading.Thread(target=pruner_loop, daemon=True)
t.start()

def stop_pruner():
stop_event.set()

Expose: start_pruner(), stop_pruner()
Start in main.py startup after Ollama check: context_pruner.start_pruner()

MODULE 3 — api/ws_manager.py (NEW FILE)
from fastapi import WebSocket

class WSManager:
def **init**(self): self.active: list[WebSocket] = []

async def connect(self, ws: WebSocket):
await ws.accept()
self.active.append(ws)

def disconnect(self, ws: WebSocket):
if ws in self.active: self.active.remove(ws)

async def broadcast(self, message: dict):
disconnected = []
for ws in self.active:
try: await ws.send_json(message)
except: disconnected.append(ws)
for ws in disconnected: self.disconnect(ws)

ws_manager = WSManager()

MODULE 4 — Update api/server.py (add WebSocket, keep all REST endpoints)
Add at bottom of existing server.py (import ws_manager from api.ws_manager):

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
await ws_manager.connect(ws)
try:
while True: await ws.receive_text() # keep-alive ping
except: ws_manager.disconnect(ws)

Update POST /command to broadcast during execution:
await ws_manager.broadcast({"type":"user_message","data":text})
[run classifier/LLM]
await ws_manager.broadcast({"type":"action","data":action_name})
[run executor]
await ws_manager.broadcast({"type":"done","data":response_text})
On error: await ws_manager.broadcast({"type":"error","data":str(e)})

Later (when llm_engine supports streaming): broadcast each token chunk as
{"type":"token","data":chunk}

install: pip install chromadb sentence-transformers

PHASE 1 END TESTS:

1. python main.py starts — .atlas_chroma folder created — no errors
2. Tell ATLAS your name → exit → restart → ask "what is my name?" →
   ATLAS answers from ChromaDB (cross-session memory working)
3. Check collections: python -c "
   import chromadb; c=chromadb.PersistentClient('.atlas_chroma')
   print([col.name for col in c.list_collections()])"
   Expected: only 'facts' and 'summaries' — no other collections, no JSON files
4. Run 10 exchanges → wait 30 min OR manually call context_pruner.compress_and_store()
   → check summaries collection count increased by 1
5. Low confidence: manually call memory.store_fact("test", 0.5, "test")
   → fact NOT stored (below 0.75 threshold)
6. WebSocket test: install wscat (npm install -g wscat)
   wscat -c ws://localhost:8000/ws
   In another terminal: atlas 'open notepad'
   → wscat receives {"type":"action","data":"open_app"} and {"type":"done",...}
7. All v1 CLI commands still work (run all Phase 3 tests again)
