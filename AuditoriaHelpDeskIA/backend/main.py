# backend/main.py
import sqlite3
import re
import sys
import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal

# --- AÑADIDO: Importaciones para Monitorización ---
from prometheus_fastapi_instrumentator import Instrumentator
from loguru import logger

# LangChain
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_ollama.llms import OllamaLLM
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain.chains import RetrievalQA
from langchain_core.runnables import RunnableBranch, RunnableLambda, RunnablePassthrough

# --- AÑADIDO: CONFIGURACIÓN DE LOGGING ESTRUCTURADO ---
logger.remove()
logger.add(sys.stdout, serialize=True, enqueue=True)

class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.log(level, record.getMessage())

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
logging.getLogger("uvicorn").handlers = [InterceptHandler()]
logging.getLogger("uvicorn.access").handlers = [InterceptHandler()]


# --- CONFIGURACIÓN Y MODELOS ---
import os
VECTOR_STORE_DIR = "vector_store"
DB_PATH = os.getenv("DB_PATH", "data/tickets.db")
app = FastAPI(title="Corporate EPIS Pilot API - Advanced Flow")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# --- AÑADIDO: INSTRUMENTACIÓN DE PROMETHEUS ---
Instrumentator().instrument(app).expose(app)


llm = OllamaLLM(model="smollm:360m", temperature=0, base_url="http://host.docker.internal:11434")
embeddings = HuggingFaceEmbeddings(model_name="intfloat/multilingual-e5-large")
vector_store = Chroma(persist_directory=VECTOR_STORE_DIR, embedding_function=embeddings)
retriever = vector_store.as_retriever()

# --- LÓGICA DE LANGCHAIN (MODIFICADA) ---
rag_prompt_template = "Usa el siguiente contexto para responder en español de forma concisa y útil a la pregunta.\nContexto: {context}\nPregunta: {question}\nRespuesta:"
rag_prompt = PromptTemplate.from_template(rag_prompt_template)
rag_chain = RetrievalQA.from_chain_type(llm=llm, chain_type="stuff", retriever=retriever, chain_type_kwargs={"prompt": rag_prompt})

def create_support_ticket(description: str) -> str:
    """Crea un ticket de soporte y devuelve un mensaje de confirmación."""
    try:
        # Asegurar que el directorio existe
        db_dir = os.path.dirname(os.path.abspath(DB_PATH))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        # Crear la tabla si no existe
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            status TEXT NOT NULL
        )
        ''')
        conn.commit()
        
        problem_description = description.replace("ACTION_CREATE_TICKET:", "").strip()
        if not problem_description:
            problem_description = "Problema no especificado por el usuario."

        cursor.execute("INSERT INTO tickets (description, status) VALUES (?, ?)", (problem_description, "Abierto"))
        ticket_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return f"De acuerdo. He creado el ticket de soporte #{ticket_id} con tu problema: '{problem_description}'. El equipo técnico se pondrá en contacto contigo."
    except Exception as e:
        logger.error(f"Error al crear ticket: {e}")
        return f"Error al crear el ticket. Por favor, inténtalo de nuevo. Error: {str(e)}"

# El router ahora es más simple
# CAMBIO 1: Añadimos la nueva intención 'despedida'
class RouteQuery(BaseModel):
    intent: Literal["pregunta_general", "reporte_de_problema", "despedida"] = Field(description="La intención del usuario.")

output_parser = JsonOutputParser(pydantic_object=RouteQuery)
# CAMBIO 2: Actualizamos el prompt para que el LLM sepa qué es una 'despedida'
# Simplificado para modelos pequeños como smollm:360m
router_prompt = PromptTemplate(
    template="""Clasifica la intención del usuario. Responde SOLO con un JSON válido con este formato exacto:
{{"intent": "valor_aqui"}}

Opciones para "intent":
- "pregunta_general": cuando el usuario pregunta algo, busca información
- "reporte_de_problema": cuando el usuario reporta un problema, algo no funciona, necesita ayuda técnica
- "despedida": cuando el usuario dice gracias, adiós, perfecto, vale, ok

Pregunta del usuario: {question}

Respuesta JSON:""",
    input_variables=["question"],
)
def extract_json_from_string(text: str) -> str:
    # Buscar JSON válido en la respuesta
    match = re.search(r'\{"intent"\s*:\s*"[^"]+"\s*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    
    # Si no encuentra JSON válido, intentar detectar la intención por palabras clave
    text_lower = text.lower()
    if any(word in text_lower for word in ['gracias', 'adiós', 'adios', 'perfecto', 'vale', 'ok', 'okay']):
        return '{"intent": "despedida"}'
    if any(word in text_lower for word in ['problema', 'no funciona', 'roto', 'averiado', 'error', 'falla', 'ticket']):
        return '{"intent": "reporte_de_problema"}'
    
    # Por defecto, asumir pregunta general
    return '{"intent": "pregunta_general"}'

router_chain = router_prompt | llm | RunnableLambda(extract_json_from_string) | output_parser

chain_with_preserved_input = RunnablePassthrough.assign(decision=router_chain)

problem_chain = RunnableLambda(lambda x: {"query": x["question"]}) | rag_chain

# --- ENDPOINT DE LA API (MODIFICADO) ---
@app.get("/ask")
def ask_question(question: str):
    try:
        if question.startswith("ACTION_CREATE_TICKET:"):
            description = question.split(":", 1)[1]
            return {"answer": create_support_ticket(description), "follow_up_required": False}

        # Intentar obtener la decisión del router
        try:
            decision_result = chain_with_preserved_input.invoke({"question": question})
            intent = decision_result["decision"]["intent"]
        except Exception as router_error:
            logger.warning(f"Error en router, usando fallback: {router_error}")
            # Fallback: detectar intención por palabras clave
            question_lower = question.lower()
            if any(word in question_lower for word in ['gracias', 'adiós', 'adios', 'perfecto', 'vale', 'ok']):
                intent = "despedida"
            elif any(word in question_lower for word in ['problema', 'no funciona', 'roto', 'averiado', 'error', 'falla', 'ticket']):
                intent = "reporte_de_problema"
            else:
                intent = "pregunta_general"
            decision_result = {"question": question}
        
        answer = ""
        follow_up = False

        if intent == "pregunta_general":
            try:
                result = problem_chain.invoke(decision_result)
                answer = result.get("result", "No se encontró respuesta en la base de conocimiento.")
            except Exception as e:
                logger.error(f"Error en RAG chain: {e}")
                answer = "No pude encontrar información relevante en la base de conocimiento."
        elif intent == "reporte_de_problema":
            try:
                result = problem_chain.invoke(decision_result)
                solution = result.get("result", "No he encontrado una solución específica en mis documentos.")
                answer = f"{solution}\n\n¿Esta información soluciona tu problema?"
                follow_up = True
            except Exception as e:
                logger.error(f"Error en RAG chain para problema: {e}")
                answer = "No pude encontrar una solución en la base de conocimiento.\n\n¿Quieres crear un ticket de soporte para que un experto te ayude?"
                follow_up = True
        elif intent == "despedida":
            answer = "De nada, ¡un placer ayudar! Si tienes cualquier otra consulta, aquí estaré. 😊"
            follow_up = False
            
        return {"answer": answer, "follow_up_required": follow_up}

    except Exception as e:
        # AÑADIDO: Usamos logger en lugar de print para un registro estructurado
        logger.error(f"Error en el endpoint /ask: {e}", exc_info=True)
        return {"answer": f"Lo siento, ha ocurrido un error. Por favor, intenta de nuevo o describe tu problema para crear un ticket de soporte.", "follow_up_required": False}