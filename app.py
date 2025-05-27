import time
import random
import json
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import threading
import queue
from datetime import datetime
from flask_socketio import SocketIO

app = Flask(__name__, template_folder='templates')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

clientes_db = {}
db_lock = threading.Lock()

cola_caja = queue.Queue()
cola_servicio_cliente = queue.Queue()
contador_caja = 1
contador_servicio = 1
contador_lock = threading.Lock()

# Nuevos estados de agentes
agentes_caja = [None, None]
agente_servicio = None

cajeros_disponibles = 2
servicio_cliente_disponibles = 1
cajeros_lock = threading.Lock()
servicio_lock = threading.Lock()

def generate_ticket(tipo_servicio):
    global contador_caja, contador_servicio
    with contador_lock:
        if tipo_servicio == 'caja':
            numero_ticket = contador_caja
            contador_caja += 1
            cola_caja.put(numero_ticket)
            posicion = cola_caja.qsize()
            tiempo_estimado = posicion * 7.5
            prefijo = 'C'
        else:
            numero_ticket = contador_servicio
            contador_servicio += 1
            cola_servicio_cliente.put(numero_ticket)
            posicion = cola_servicio_cliente.qsize()
            tiempo_estimado = posicion * 11
            prefijo = 'S'

    ticket_formateado = f"{prefijo}{numero_ticket:03d}"
    return ticket_formateado, posicion, int(tiempo_estimado)

def procesar_caja(numero_ticket, agente_id):
    global agentes_caja
    tiempo_atencion = random.randint(5, 10)
    agentes_caja[agente_id] = f"C{numero_ticket:03d}"
    socketio.emit('agente_update', get_agente_status())
    time.sleep(tiempo_atencion)
    agentes_caja[agente_id] = None
    with cajeros_lock:
        global cajeros_disponibles
        cajeros_disponibles += 1
    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

def procesar_servicio(numero_ticket):
    global agente_servicio
    tiempo_atencion = random.randint(7, 15)
    agente_servicio = f"S{numero_ticket:03d}"
    socketio.emit('agente_update', get_agente_status())
    time.sleep(tiempo_atencion)
    agente_servicio = None
    with servicio_lock:
        global servicio_cliente_disponibles
        servicio_cliente_disponibles += 1
    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

def monitor_cola_caja():
    while True:
        if not cola_caja.empty():
            with cajeros_lock:
                for i in range(len(agentes_caja)):
                    if cajeros_disponibles > 0 and agentes_caja[i] is None:
                        cajeros_disponibles -= 1
                        ticket = cola_caja.get()
                        threading.Thread(target=procesar_caja, args=(ticket, i)).start()
                        break
        time.sleep(1)

def monitor_cola_servicio():
    while True:
        if not cola_servicio_cliente.empty():
            with servicio_lock:
                if servicio_cliente_disponibles > 0 and agente_servicio is None:
                    servicio_cliente_disponibles -= 1
                    ticket = cola_servicio_cliente.get()
                    threading.Thread(target=procesar_servicio, args=(ticket,)).start()
        time.sleep(1)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ticket', methods=['POST'])
def external_ticket():
    data = request.json
    if not data or 'tipo_servicio' not in data:
        return jsonify({'error': 'Invalid request'}), 400

    cliente_id = data.get('cliente_id', f"EXT-{random.randint(1000,9999)}")
    ticket, pos, wait = generate_ticket(data['tipo_servicio'])

    with db_lock:
        clientes_db[cliente_id] = {
            'ticket': ticket,
            'status': 'waiting',
            'timestamp': datetime.now().isoformat(),
            'service': data['tipo_servicio']
        }

    socketio.emit('queue_update', get_queue_status())
    return jsonify({
        'ticket': ticket,
        'position': pos,
        'wait_time': wait,
        'cliente_id': cliente_id
    })

@app.route('/api/status/<cliente_id>', methods=['GET'])
def client_status(cliente_id):
    with db_lock:
        if cliente_id in clientes_db:
            return jsonify(clientes_db[cliente_id])
    return jsonify({'error': 'Client not found'}), 404

@app.route('/estado-colas')
def estado_colas():
    return jsonify(get_queue_status())

@app.route('/estado-agentes')
def estado_agentes():
    return jsonify(get_agente_status())

def get_queue_status():
    return {
        'caja': {
            'waiting': cola_caja.qsize(),
            'available': cajeros_disponibles
        },
        'servicio_cliente': {
            'waiting': cola_servicio_cliente.qsize(),
            'available': servicio_cliente_disponibles
        }
    }

def get_agente_status():
    return {
        'agentes_caja': agentes_caja,
        'agente_servicio': agente_servicio
    }

if __name__ == '__main__':
    threading.Thread(target=monitor_cola_caja, daemon=True).start()
    threading.Thread(target=monitor_cola_servicio, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=10000)
