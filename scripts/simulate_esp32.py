#!/usr/bin/env python3
"""Simulador do ESP32 (Freezer / Câmara Fria) para testes de bancada e homologação.

Permite testar sem o hardware físico:
  1. Envio de telemetria normal (ex: -3.5 °C)
  2. Disparo de alerta de temperatura fora da faixa ideal (ex: -15.0 °C ou +11.0 °C)
  3. Simulação de queda abrupta de energia via Last Will and Testament (--simulate-lwt)
"""

import argparse
from datetime import datetime, timezone
import json
import random
import time
import uuid

import paho.mqtt.client as mqtt


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulador MQTT do ESP32 - Coutos Conveniência")
    parser.add_argument("--host", default="localhost", help="Host do Broker MQTT (default: localhost)")
    parser.add_argument("--port", type=int, default=1884, help="Porta do Broker MQTT (default: 1884)")
    parser.add_argument("--device-id", default="camara01", help="ID do equipamento (default: camara01)")
    parser.add_argument(
        "--temp",
        type=float,
        default=None,
        help="Temperatura fixa em °C para enviar (ex: -15.0 ou 9.5). Se omitido, oscila entre -4°C e -1°C",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Envia leituras continuamente a cada --interval segundos",
    )
    parser.add_argument("--interval", type=int, default=10, help="Intervalo em segundos no modo --loop")
    parser.add_argument(
        "--simulate-lwt",
        action="store_true",
        help="Publica evento de desconexão abrupta (LWT 'offline') para testar o alerta de queda",
    )
    args = parser.parse_args()

    topic_base = f"loja/freezer/{args.device_id}"
    topic_telemetry = f"{topic_base}/telemetry"
    topic_status = f"{topic_base}/status"

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"sim_esp32_{args.device_id}_{uuid.uuid4().hex[:4]}",
    )
    client.will_set(topic_status, payload="offline", qos=1, retain=True)

    print(f"[SIMULADOR] Conectando ao Broker MQTT em {args.host}:{args.port}...")
    client.connect(args.host, args.port, keepalive=15)
    client.loop_start()
    time.sleep(0.3)

    if args.simulate_lwt:
        print(f"[SIMULADOR LWT] Publicando 'offline' em [{topic_status}]...")
        info = client.publish(topic_status, payload="offline", qos=1, retain=True)
        info.wait_for_publish()
        print("[SIMULADOR LWT] Evento de queda de energia enviado com sucesso!")
        client.loop_stop()
        client.disconnect()
        return

    # Marca o dispositivo como online
    client.publish(topic_status, payload="online", qos=1, retain=True).wait_for_publish()

    try:
        seq = 1
        while True:
            temp_c = args.temp if args.temp is not None else round(random.uniform(-4.5, -1.0), 2)
            payload = {
                "event_id": f"{args.device_id}_{int(time.time() * 1000)}_{seq}",
                "device_id": args.device_id,
                "temperature_c": temp_c,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            info = client.publish(topic_telemetry, payload=json.dumps(payload), qos=1)
            info.wait_for_publish()
            print(f"[SIMULADOR PUB] {topic_telemetry} -> Temp={temp_c:.2f}°C | event_id={payload['event_id']}")

            if not args.loop:
                break
            seq += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[SIMULADOR] Encerrando...")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
