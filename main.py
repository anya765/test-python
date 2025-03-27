from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
import socket
import time
import random
from enum import Enum
import threading
import json
from typing import Dict, Optional, List

app = FastAPI(title="Hardware-in-the-Loop Simulator API")

# Global variables to control simulation
simulation_running = False
simulation_thread = None
stop_event = threading.Event()

# Default configuration values
DEFAULT_HOST = "192.168.1.100"  # Must match ETH_SERVER_IP in config.h
DEFAULT_PORT = 8080            # Must match ETH_SERVER_PORT in config.h
 
class SensorType(str, Enum):
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"
    FORCE = "force"
    ACCELERATION = "acceleration"
    POSITION = "position"
    LIGHT = "light"

class SimulationConfig(BaseModel):
    sensor_type: SensorType = SensorType.TEMPERATURE
    min_value: float = 0.0
    max_value: float = 100.0
    noise_factor: float = 0.1
    sample_rate_hz: float = 1.0  # Samples per second
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

class SimulationStatus(BaseModel):
    running: bool
    sensor_type: Optional[SensorType] = None
    sample_rate_hz: Optional[float] = None
    uptime_seconds: Optional[float] = None

# Store active configurations
active_config = SimulationConfig()
start_time = None

def generate_sensor_data(sensor_type: SensorType, min_val: float, max_val: float, noise: float) -> Dict:
    """Generate fake sensor data based on type and parameters"""
    base_value = min_val + (random.random() * (max_val - min_val))
    noise_value = (random.random() * 2 - 1) * noise * (max_val - min_val)
    value = base_value + noise_value
    
    timestamp = time.time()
    
    # Format that the C firmware can parse (simple key-value pairs)
    return {
        "type": sensor_type,
        "value": round(value, 3),
        "timestamp": timestamp,
        "unit": get_unit_for_sensor(sensor_type)
    }

def get_unit_for_sensor(sensor_type: SensorType) -> str:
    """Return the appropriate unit for a sensor type"""
    units = {
        SensorType.TEMPERATURE: "C",
        SensorType.PRESSURE: "kPa",
        SensorType.FORCE: "N",
        SensorType.ACCELERATION: "m/s²",
        SensorType.POSITION: "mm",
        SensorType.LIGHT: "lux"
    }
    return units.get(sensor_type, "")

def simulation_worker(config: SimulationConfig, stop_event: threading.Event):
    """Background worker that generates and sends sensor data"""
    global start_time
    start_time = time.time()
    
    sock = None
    try:
        # Create TCP server socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.host, config.port))
        sock.listen(1)
        sock.settimeout(1)  # 1 second timeout for accept()
        
        print(f"Server started on {config.host}:{config.port}")
        
        while not stop_event.is_set():
            try:
                # Wait for connection from the C client
                client_sock, addr = sock.accept()
                print(f"Connection from {addr}")
                
                try:
                    while not stop_event.is_set():
                        # Generate sensor data
                        data = generate_sensor_data(
                            config.sensor_type,
                            config.min_value,
                            config.max_value,
                            config.noise_factor
                        )
                        
                        # Convert to string and send
                        data_str = json.dumps(data) + "\n"
                        client_sock.sendall(data_str.encode('utf-8'))
                        
                        # Receive any response
                        try:
                            client_sock.settimeout(0.1)
                            response = client_sock.recv(1024)
                            if response:
                                print(f"Received: {response.decode('utf-8').strip()}")
                        except socket.timeout:
                            pass
                        
                        # Wait according to sample rate
                        time.sleep(1.0 / config.sample_rate_hz)
                        
                finally:
                    client_sock.close()
                    
            except socket.timeout:
                # Timeout on accept, check if we need to stop
                continue
            except Exception as e:
                print(f"Error in client connection: {e}")
                time.sleep(1)
    
    except Exception as e:
        print(f"Error in simulation thread: {e}")
    finally:
        if sock:
            sock.close()
        print("Simulation stopped")

@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "message": "HIL Sensor Simulator API",
        "endpoints": [
            "/start - Start the simulation",
            "/stop - Stop the simulation",
            "/status - Get simulation status",
            "/config - Get or update simulation config"
        ]
    }

@app.post("/start")
async def start_simulation(background_tasks: BackgroundTasks):
    """Start the sensor data simulation"""
    global simulation_running, simulation_thread, stop_event
    
    if simulation_running:
        return {"message": "Simulation already running"}
    
    # Reset the stop event
    stop_event.clear()
    
    # Start the simulation in a background thread
    simulation_running = True
    simulation_thread = threading.Thread(
        target=simulation_worker,
        args=(active_config, stop_event)
    )
    simulation_thread.daemon = True
    simulation_thread.start()
    
    return {"message": "Simulation started", "config": active_config}

@app.post("/stop")
async def stop_simulation():
    """Stop the sensor data simulation"""
    global simulation_running, simulation_thread, stop_event
    
    if not simulation_running:
        return {"message": "No simulation running"}
    
    # Signal the thread to stop
    stop_event.set()
    
    # Wait for thread to complete
    if simulation_thread:
        simulation_thread.join(timeout=5.0)
    
    simulation_running = False
    return {"message": "Simulation stopped"}

@app.get("/status")
async def get_status():
    """Get current simulation status"""
    global simulation_running, active_config, start_time
    
    if not simulation_running:
        return {"running": False}
    
    uptime = time.time() - start_time if start_time else 0
    
    return SimulationStatus(
        running=simulation_running,
        sensor_type=active_config.sensor_type,
        sample_rate_hz=active_config.sample_rate_hz,
        uptime_seconds=round(uptime, 1)
    )

@app.get("/config")
async def get_config():
    """Get current simulation configuration"""
    global active_config
    return active_config

@app.post("/config")
async def update_config(config: SimulationConfig):
    """Update simulation configuration"""
    global active_config, simulation_running
    
    # If simulation is running, don't allow config changes
    if simulation_running:
        raise HTTPException(
            status_code=400,
            detail="Cannot update configuration while simulation is running. Stop the simulation first."
        )
    
    active_config = config
    return {"message": "Configuration updated", "config": active_config}

@app.get("/generate-sample")
async def generate_sample():
    """Generate a single sample of sensor data for testing"""
    global active_config
    
    data = generate_sensor_data(
        active_config.sensor_type,
        active_config.min_value,
        active_config.max_value,
        active_config.noise_factor
    )
    
    return data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)