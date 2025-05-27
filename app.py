import time
import random
import json
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import threading
import queue
from datetime import datetime
from flask_socketio import SocketIO

app = Flask(__name__, template_folder='templates')
app.secret_key = 'clave_secreta_segura'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

clientes_db = {}
db_lock = threading.Lock()

cola_caja = queue.Queue()
cola_servicio_cliente = queue.Queue()
contador_caja = 1
contador_servicio = 1
contador_lock = threading.Lock()

agentes_caja = [None, None]
agente_servicio = None

cajeros_disponibles = 2
servicio_cliente_disponibles = 1
cajeros_lock = threading.Lock()
servicio_lock = threading.Lock()

ADMIN_USER = 'admin'
ADMIN_PASS = 'admin123'

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
    global agentes_caja, cajeros_disponibles
    tiempo_atencion = random.randint(5, 10)
    ticket_id = f"C{numero_ticket:03d}"
    agentes_caja[agente_id] = ticket_id
    socketio.emit('agente_update', {
        'agentes_caja': agentes_caja,
        'agente_servicio': agente_servicio,
        'duraciones': {
            f"caja{agente_id+1}": tiempo_atencion
        }
    })
    time.sleep(tiempo_atencion)
    agentes_caja[agente_id] = None
    with cajeros_lock:
        cajeros_disponibles += 1
    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

def procesar_servicio(numero_ticket):
    global agente_servicio, servicio_cliente_disponibles
    tiempo_atencion = random.randint(7, 15)
    ticket_id = f"S{numero_ticket:03d}"
    agente_servicio = ticket_id
    socketio.emit('agente_update', {
        'agentes_caja': agentes_caja,
        'agente_servicio': agente_servicio,
        'duraciones': {
            "servicio": tiempo_atencion
        }
    })
    time.sleep(tiempo_atencion)
    agente_servicio = None
    with servicio_lock:
        servicio_cliente_disponibles += 1
    socketio.emit('queue_update', get_queue_status())
    socketio.emit('agente_update', get_agente_status())

def monitor_cola_caja():
    global cajeros_disponibles
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
    global servicio_cliente_disponibles
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USER and password == ADMIN_PASS:
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

@app.route('/api/ticket', methods=['POST'])
def external_ticket():
    data = request.json
    if not data or 'tipo_servicio' not in data:
        return jsonify({'error': 'Solicitud inválida'}), 400

    tipo = data['tipo_servicio']
    if tipo not in ['caja', 'servicio_cliente']:
        return jsonify({'error': 'Tipo de servicio no válido'}), 400

    cliente_id = data.get('cliente_id', f"EXT-{random.randint(1000,9999)}")
    ticket, pos, wait = generate_ticket(tipo)

    with db_lock:
        clientes_db[cliente_id] = {
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
        'cliente_id': cliente_id
    })

@app.route('/api/status/<cliente_id>', methods=['GET'])
def client_status(cliente_id):
    with db_lock:
        if cliente_id in clientes_db:
            return jsonify(clientes_db[cliente_id])
    return jsonify({'error': 'Cliente no encontrado'}), 404

@app.route('/estado-colas')
def estado_colas():
    return jsonify(get_queue_status())

@app.route('/estado-agentes')
def estado_agentes():
    return jsonify(get_agente_status())

@app.route('/tickets-por-agente')
def tickets_por_agente():
    return jsonify({
        'caja': agentes_caja,
        'servicio_cliente': agente_servicio
    })

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
