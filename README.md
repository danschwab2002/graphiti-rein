# Graphiti Memory API — RE(IN) app

Microservicio de memoria a largo plazo basado en Graphiti + Neo4j.
Se integra con el flujo de n8n mediante dos llamadas HTTP simples.

---

## Estructura del repo

```
graphiti-api/
├── main.py           # Aplicación FastAPI
├── requirements.txt  # Dependencias Python
├── Dockerfile        # Imagen del contenedor
├── .env.example      # Variables de entorno de referencia
└── README.md
```

---

## Deploy en Easypanel

### Paso 1 — Levantar Neo4j

En Easypanel → **New Service → Docker Image**

- Image: `neo4j:5-community`
- Variables de entorno:
  ```
  NEO4J_AUTH=neo4j/cambia_esta_password_segura
  NEO4J_PLUGINS=["apoc"]
  NEO4J_dbms_memory_heap_max__size=512m
  ```
- Puertos internos: `7687` (Bolt), `7474` (UI web, opcional)
- Volumen persistente: `/data` → para que los datos sobrevivan reinicios

> Guardá bien la password. Tiene que coincidir exactamente con `NEO4J_PASSWORD` del microservicio.

### Paso 2 — Levantar el microservicio Graphiti

En Easypanel → **New Service → GitHub**

- Seleccioná el repo donde subiste este código
- Branch: `main`
- Easypanel detectará el `Dockerfile` automáticamente

Variables de entorno a configurar:
```
NEO4J_URI=bolt://[nombre-del-servicio-neo4j]:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=cambia_esta_password_segura
OPENAI_API_KEY=sk-...
API_SECRET=tu_token_secreto_random
```

> Para `NEO4J_URI`: en Easypanel los servicios se comunican por nombre interno.
> Si llamaste al servicio Neo4j `neo4j`, la URI es `bolt://neo4j:7687`.

- Puerto: `8000`
- No es necesario exponerlo a internet. Solo necesita ser accesible internamente desde n8n.

### Paso 3 — Verificar que funciona

Desde n8n (o Postman), hacé un GET a:
```
http://[nombre-servicio-graphiti]:8000/health
```

Debe devolver: `{"status": "ok"}`

---

## Integración en n8n

### Estructura del flujo

```
Webhook → HTTP /search → Agente Claude → HTTP /add-episode → Respuesta
```

### Nodo: HTTP Request → /search (antes de Claude)

- Method: `POST`
- URL: `http://graphiti-api:8000/search`
- Body (JSON):
```json
{
  "query": "{{ $json.chatInput }}",
  "user_id": "{{ $json.userId }}",
  "secret": "tu_token_secreto_random",
  "num_results": 10
}
```
- El output `{{ $json.context }}` se inyecta en el system prompt de Claude.

### Nodo: Agente Claude

En el system prompt, agregá al principio:

```
{{ $('HTTP Request - Search').item.json.context }}

Sos el copiloto de negocios del RE(IN) app. Usá el historial del alumno 
para dar respuestas contextualizadas. Si el alumno cambió de nicho u opinión 
en algún momento, tené en cuenta esa evolución.
```

### Nodo: HTTP Request → /add-episode (después de Claude)

- Method: `POST`
- URL: `http://graphiti-api:8000/add-episode`
- Body (JSON):
```json
{
  "messages": [
    {"role": "user", "content": "{{ $json.chatInput }}"},
    {"role": "assistant", "content": "{{ $json.output }}"}
  ],
  "user_id": "{{ $json.userId }}",
  "secret": "tu_token_secreto_random"
}
```

---

## Notas importantes

**user_id**: debe ser un identificador único y consistente por alumno. 
Puede ser el email, el ID de la base de datos de RE(IN), o cualquier string estable.
Graphiti aísla completamente las memorias por `user_id` (internamente usa `group_id`).

**Primera ejecución**: Graphiti crea los índices en Neo4j automáticamente al arrancar.
El primer request puede tardar unos segundos más. A partir del segundo, todo es normal.

**Ver el grafo**: Si habilitaste el puerto 7474 de Neo4j, podés acceder a la UI web
en `http://tu-vps:7474` con las credenciales neo4j/password. Ahí podés ver
visualmente cómo Graphiti está construyendo el grafo de cada alumno.

**Actualizar el microservicio**: Hacés push al repo de GitHub y en Easypanel
apretás "Redeploy". Neo4j no se toca, los datos persisten.
