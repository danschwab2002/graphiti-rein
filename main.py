import os
import json
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
NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USER     = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
API_SECRET     = os.environ.get("API_SECRET", "")

os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY


# ── Monkey-patch directo en los módulos de operaciones ────────────────────────
import graphiti_core.utils.maintenance.node_operations as node_ops

from graphiti_core.prompts.models import Message as PromptMessage


async def rein_extract_message_nodes(llm_client, episode, previous_episodes):
    """Reemplaza extract_message_nodes con prompts personalizados para RE(IN)"""

    context = {
        'episode_content': episode.content,
        'episode_timestamp': episode.valid_at.isoformat(),
        'previous_episodes': [
            {
                'content': ep.content,
                'timestamp': ep.valid_at.isoformat(),
            }
            for ep in previous_episodes
        ],
    }

    sys_prompt = """Sos un sistema de extracción de entidades para RE(IN), 
una incubadora de negocios digitales.

Tu tarea es identificar las entidades relevantes de una conversación 
entre un alumno emprendedor y su copiloto de IA.

EXTRAE entidades como:
- El alumno (siempre extrae al alumno como entidad principal)
- Negocios, proyectos o emprendimientos mencionados
- Nichos o industrias específicas mencionadas
- Herramientas, plataformas o canales mencionados
- Clientes, mentores o socios mencionados

NO EXTRAIGAS:
- Datos estáticos como nombre, edad o ubicación del alumno
- Conceptos teóricos del programa RE(IN)
- Relaciones o acciones entre entidades
- Fechas o información temporal"""

    user_prompt = f"""
Conversación previa:
{json.dumps([ep['content'] for ep in context['previous_episodes']], indent=2)}

Mensaje actual:
{context['episode_content']}

Extraé las entidades relevantes del mensaje actual.

Respondé con un JSON en este formato:
{{
    "extracted_nodes": [
        {{
            "name": "Nombre único de la entidad",
            "labels": ["Entity"],
            "summary": "Descripción breve del rol de esta entidad"
        }}
    ]
}}
"""
    messages = [
        PromptMessage(role='system', content=sys_prompt),
        PromptMessage(role='user', content=user_prompt),
    ]

    llm_response = await llm_client.generate_response(messages)
    return llm_response.get('extracted_nodes', [])


# Reemplazamos directamente en el módulo
node_ops.extract_message_nodes = rein_extract_message_nodes

logger.info("Prompts personalizados de RE(IN) aplicados correctamente.")


# ── Instancia global de Graphiti ──────────────────────────────────────────────
graphiti: Graphiti = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graphiti
    logger.info("Inicializando Graphiti...")
    graphiti = Graphiti(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    await graphiti.build_indices_and_constraints()
    logger.info("Graphiti listo.")
    yield
    logger.info("Cerrando conexión con Neo4j...")
    await graphiti.close()


app = FastAPI(
    title="Graphiti Memory API",
    version="3.0.0",
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
    role: str
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
    Busca memorias episódicas relevantes de un usuario dado su mensaje actual.
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

        facts_with_dates = []
        for r in results:
            if not r.fact:
                continue

            date_str = ""
            if hasattr(r, 'valid_at') and r.valid_at:
                date_str = f" (desde {r.valid_at.strftime('%B %Y')})"
                if hasattr(r, 'invalid_at') and r.invalid_at:
                    date_str = f" ({r.valid_at.strftime('%B %Y')} - {r.invalid_at.strftime('%B %Y')})"

            facts_with_dates.append(f"- {r.fact}{date_str}")

        context = "MEMORIA EPISÓDICA DEL ALUMNO:\n"
        context += "\n".join(facts_with_dates)

        return {"context": context, "facts": [r.fact for r in results if r.fact]}

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
        episode_body = "\n".join(
            f"{msg.role.upper()}: {msg.content}"
            for msg in req.messages
        )

        await graphiti.add_episode(
            name=f"conv_{req.user_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            episode_body=episode_body,
            source_description="Conversación de coaching entre alumno emprendedor y copiloto RE(IN). Extraer decisiones de negocio, cambios de dirección, acciones confirmadas por el alumno, contenido del programa trabajado, y evolución del negocio. NO extraer recomendaciones del asistente que el alumno no haya confirmado.",
            reference_time=datetime.now(timezone.utc),
            source=EpisodeType.message,
            group_id=req.user_id,
        )

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en /add-episode: {e}")
        raise HTTPException(status_code=500, detail=str(e))