import random
import json
import threading
import queue
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
from flask_socketio import SocketIO

app = Flask(__name__, template_folder='templates')
app.secret_key = 'clave_secreta_segura'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ----- In-Memory "DB" and Locks -----
clientes_db = {}
db_lock = threading.Lock()

cola_caja = queue.Queue()
cola_servicio_cliente = queue.Queue()

contador_caja = 1
contador_servicio = 1
contador_lock = threading.Lock()

# ----- Agentes y Disponibilidad -----
agentes_caja = [None, None]
agente_servicio = None

cajeros_disponibles = 2
servicio_cliente_disponibles = 1
cajeros_lock = threading.Lock()
servicio_lock = threading.Lock()

# ----- Credenciales Admin -----
ADMIN_USER = 'admin'
ADMIN_PASS = 'admin123'

# ----- Generación de Tickets -----
def generate_ticket(tipo_servicio):
    global contador_caja, contador_servicio
    with contador_lock:
        if tipo_servicio == 'caja':
            n = contador_caja
            contador_caja += 1
            cola_caja.put(n)
            pos = cola_caja.qsize()
            wait_time = pos * 7.5
            prefix = 'C'
        else:
            n = contador_servicio
            contador_servicio += 1
            cola_servicio_cliente.put(n)
            pos = cola_servicio_cliente.qsize()
            wait_time = pos * 11
            prefix = 'S'
    ticket = f"{prefix}{n:03d}"
    return ticket, pos, int(wait_time)

# ----- Procesamiento de Cajeros -----
def procesar_caja(nro, agente_id):
    global agentes_caja, cajeros_disponibles
    duracion = random.randint(5, 10)
    ticket = f"C{nro:03d}"
    agentes_caja[agente_id] = ticket

    socketio.emit('agente_update', {
        'agentes_caja': agentes_caja,
        'agente_servicio': agente_servicio,
        'duraciones': { f'caja{agente_id+1}': duracion }
    })

    socketio.sleep(duracion)   # No bloquear el event loop
    agentes_caja[agente_id] = None

    with cajeros_lock:
        cajeros_disponibles += 1

    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

# ----- Procesamiento de Servicio al Cliente -----
def procesar_servicio(nro):
    global agente_servicio, servicio_cliente_disponibles
    duracion = random.randint(7, 15)
    ticket = f"S{nro:03d}"
    agente_servicio = ticket

    socketio.emit('agente_update', {
        'agentes_caja': agentes_caja,
        'agente_servicio': agente_servicio,
        'duraciones': { 'servicio': duracion }
    })

    socketio.sleep(duracion)
    agente_servicio = None

    with servicio_lock:
        servicio_cliente_disponibles += 1

    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

# ----- Monitores en Background -----
def monitor_cola_caja():
    global cajeros_disponibles
    while True:
        if not cola_caja.empty():
            with cajeros_lock:
                for i in range(len(agentes_caja)):
                    if cajeros_disponibles > 0 and agentes_caja[i] is None:
                        cajeros_disponibles -= 1
                        nro = cola_caja.get()
                        socketio.start_background_task(procesar_caja, nro, i)
                        break
        socketio.sleep(1)

def monitor_cola_servicio():
    global servicio_cliente_disponibles
    while True:
        if not cola_servicio_cliente.empty():
            with servicio_lock:
                if servicio_cliente_disponibles > 0 and agente_servicio is None:
                    servicio_cliente_disponibles -= 1
                    nro = cola_servicio_cliente.get()
                    socketio.start_background_task(procesar_servicio, nro)
        socketio.sleep(1)

# ----- Rutas Web -----
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        if u == ADMIN_USER and p == ADMIN_PASS:
            session['admin'] = True
            return redirect(url_for('admin_panel'))
        return render_template('login.html', error='Credenciales incorrectas')
    return render_template('login.html')

@app.route('/admin')
def admin_panel():
    if not session.get('admin'):
        return redirect(url_for('login'))
    return render_template('admin.html', clientes=clientes_db)

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

# ----- APIs Cliente -----
@app.route('/api/ticket', methods=['POST'])
def api_ticket():
    data = request.json or {}
    tipo = data.get('tipo_servicio')
    if tipo not in ('caja','servicio_cliente'):
        return jsonify({'error':'Tipo de servicio no válido'}), 400

    client_id = data.get('cliente_id', f"EXT-{random.randint(1000,9999)}")
    ticket, pos, wait = generate_ticket(tipo)

    with db_lock:
        clientes_db[client_id] = {
            'ticket': ticket,
            'status': 'waiting',
            'timestamp': datetime.now().isoformat(),
            'service': tipo
        }

    socketio.emit('queue_update', get_queue_status())
    return jsonify({
        'ticket': ticket,
        'position': pos,
        'wait_time': wait,
        'cliente_id': client_id
    })

@app.route('/api/status/<cliente_id>')
def api_status(cliente_id):
    with db_lock:
        cliente = clientes_db.get(cliente_id)
        if not cliente:
            return jsonify({'error':'Cliente no encontrado'}), 404
        return jsonify(cliente)

# ----- Endpoints de Estado -----
@app.route('/estado-colas')
def estado_colas():
    return jsonify(get_queue_status())

@app.route('/estado-agentes')
def estado_agentes():
    return jsonify(get_agente_status())

@app.route('/tickets-por-agente')
def tickets_por_agente():
    return jsonify({'caja': agentes_caja, 'servicio_cliente': agente_servicio})

# ----- Helpers -----
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

# ----- Arranque del Servidor -----
if __name__ == '__main__':
    socketio.start_background_task(monitor_cola_caja)
    socketio.start_background_task(monitor_cola_servicio)
    socketio.run(app, host='0.0.0.0', port=10000)
