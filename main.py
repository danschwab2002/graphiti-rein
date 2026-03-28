import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuración desde variables de entorno ──────────────────────────────────
NEO4J_URI      = os.environ["NEO4J_URI"]       # bolt://neo4j:7687
NEO4J_USER     = os.environ["NEO4J_USER"]      # neo4j
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]  # tu_password_segura
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]  # key fija de la plataforma
API_SECRET     = os.environ.get("API_SECRET", "")  # token interno para proteger el endpoint

os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

# ── Instancia global de Graphiti (se inicializa una sola vez al arrancar) ─────
graphiti: Graphiti = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graphiti
    logger.info("Inicializando Graphiti...")
    graphiti = Graphiti(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    # Crea los índices necesarios en Neo4j (solo hace algo la primera vez)
    await graphiti.build_indices_and_constraints()
    logger.info("Graphiti listo.")
    yield
    logger.info("Cerrando conexión con Neo4j...")
    await graphiti.close()


app = FastAPI(
    title="Graphiti Memory API",
    description="Microservicio de memoria a largo plazo para el RE(IN) app",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def verify_secret(secret: str):
    if API_SECRET and secret != API_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")


# ── Modelos de request ────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    user_id: str
    secret: str = ""
    num_results: int = 10


class Message(BaseModel):
    role: str   # "user" o "assistant"
    content: str


class AddEpisodeRequest(BaseModel):
    messages: list[Message]
    user_id: str
    secret: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/search")
async def search(req: SearchRequest):
    """
    Busca memorias relevantes de un usuario dado su mensaje actual.
    Llamar ANTES del nodo de Claude en n8n.
    Devuelve un bloque de texto listo para inyectar en el system prompt.
    """
    verify_secret(req.secret)

    try:
        results = await graphiti.search(
            query=req.query,
            group_ids=[req.user_id],
            num_results=req.num_results,
        )

        if not results:
            return {"context": "", "facts": []}

        facts = [r.fact for r in results if r.fact]

        # Bloque de texto listo para el system prompt de Claude
        context = "MEMORIA DEL ALUMNO (historial y contexto relevante):\n"
        context += "\n".join(f"- {fact}" for fact in facts)

        return {"context": context, "facts": facts}

    except Exception as e:
        logger.error(f"Error en /search: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/add-episode")
async def add_episode(req: AddEpisodeRequest):
    """
    Guarda el intercambio de mensajes en el grafo de memoria del usuario.
    Llamar DESPUÉS de que Claude respondió en n8n.
    """
    verify_secret(req.secret)

    try:
        # Convertir mensajes al formato de texto que Graphiti procesa
        episode_body = "\n".join(
            f"{msg.role.upper()}: {msg.content}"
            for msg in req.messages
        )

        await graphiti.add_episode(
            name=f"conv_{req.user_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            episode_body=episode_body,
            source_description="Conversación con el copiloto RE(IN)",
            reference_time=datetime.now(timezone.utc),
            source=EpisodeType.message,
            group_id=req.user_id,
        )

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en /add-episode: {e}")
        raise HTTPException(status_code=500, detail=str(e))
