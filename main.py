import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.models import Message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USER     = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
API_SECRET     = os.environ.get("API_SECRET", "")

os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY


# ── Prompts personalizados para RE(IN) ────────────────────────────────────────

def rein_extract_nodes(context: dict) -> list[Message]:
    sys_prompt = """Sos un sistema de extracción de entidades para RE(IN), 
una incubadora de negocios digitales.

Tu tarea es identificar las entidades relevantes de una conversación 
entre un alumno emprendedor y su copiloto de IA.

EXTRAE entidades como:
- El alumno (siempre extrae al alumno como entidad principal)
- Negocios, proyectos, emprendimientos mencionados
- Nichos o industrias específicas mencionadas
- Herramientas, plataformas o canales mencionados
- Mentores, clientes, socios mencionados

NO EXTRAIGAS:
- Datos estáticos de identidad como nombre, edad o ubicación del alumno
  (esos se manejan en otro sistema)
- Conceptos teóricos o educativos del programa RE(IN)
- Relaciones o acciones entre entidades (esas van en los edges)
- Fechas o información temporal (van en los edges)"""

    user_prompt = f"""
Conversación previa:
{[ep['content'] for ep in context['previous_episodes']]}

Mensaje actual:
{context['episode_content']}

Extraé las entidades relevantes del mensaje actual considerando 
el contexto de la conversación previa.

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
    return [
        Message(role='system', content=sys_prompt),
        Message(role='user', content=user_prompt),
    ]


def rein_extract_edges(context: dict) -> list[Message]:
    sys_prompt = """Sos un sistema de extracción de relaciones y hechos 
para RE(IN), una incubadora de negocios digitales.

Tu tarea es identificar los hechos y relaciones relevantes de una 
conversación entre un alumno emprendedor y su copiloto de IA.

EXTRAE hechos como:
- Decisiones de negocio tomadas por el alumno
- Cambios de nicho, pivots o cambios de dirección
- Estado actual del negocio (tiene clientes, está prospectando, etc.)
- Acciones que el alumno confirmó que está haciendo o hizo
- Contenido del programa que el alumno trabajó o consumió
- Dificultades o bloqueos que el alumno mencionó
- Logros o avances concretos del alumno

NO EXTRAIGAS como hechos:
- Recomendaciones del asistente que el alumno no confirmó
- Sugerencias hipotéticas o condicionales
- Conceptos teóricos explicados por el asistente
- Datos estáticos de identidad (nombre, edad, ubicación)"""

    user_prompt = f"""
Nodos disponibles:
{context['nodes']}

Episodios previos:
{[ep['content'] for ep in context['previous_episodes']]}

Episodio actual:
{context['episode_content']}

Extraé los hechos y relaciones del episodio actual.

IMPORTANTE: Solo extraé hechos que el ALUMNO confirmó explícitamente.
Si el asistente recomendó algo pero el alumno no lo confirmó → no lo extraigas.

Respondé con un JSON en este formato:
{{
    "edges": [
        {{
            "relation_type": "TIPO_RELACION_EN_MAYUSCULAS",
            "source_node_uuid": "uuid del nodo origen",
            "target_node_uuid": "uuid del nodo destino",
            "fact": "descripción concreta del hecho con contexto temporal si aplica",
            "valid_at": "YYYY-MM-DDTHH:MM:SSZ o null",
            "invalid_at": "YYYY-MM-DDTHH:MM:SSZ o null"
        }}
    ]
}}
"""
    return [
        Message(role='system', content=sys_prompt),
        Message(role='user', content=user_prompt),
    ]


# ── Monkey-patch del prompt_library ──────────────────────────────────────────
# Reemplazamos las funciones nativas con las personalizadas para RE(IN)
# Esto afecta a todos los módulos que importan prompt_library

prompt_library.extract_nodes.v2.func = rein_extract_nodes
prompt_library.extract_edges.v2.func = rein_extract_edges

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


def verify_secret(secret: str):
    if API_SECRET and secret != API_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/search")
async def search(req: SearchRequest):
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