#!/usr/bin/env python3
"""Monta a topologia Mininet, inicia Ryu se necessário e executa o experimento."""
import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.log import setLogLevel, info


BASE_DIR = Path(__file__).resolve().parent
RYU_APP = BASE_DIR / "ryu_controller.py"
RYU_LOG = BASE_DIR / "ryu.log"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Experimento SDN: descoberta, encaminhamento e medição FCT"
    )
    parser.add_argument("--topology", choices=["torus", "spineleaf"], required=True,
                        help="Topologia definida em torus.txt ou spineleaf.txt")
    parser.add_argument("--routing", choices=["ospf", "ecmp"], required=True,
                        help="ospf=menor caminho; ecmp=próximos saltos de mesmo custo")
    parser.add_argument("--exp", type=int, choices=[1, 2], required=True,
                        help="1=tráfego normal; 2=injeta uma falha de enlace")
    parser.add_argument("--controller-ip", default="127.0.0.1",
                        help="IP do controlador Ryu (padrão: 127.0.0.1)")
    parser.add_argument("--controller-port", type=int, default=6633,
                        help="Porta OpenFlow do Ryu (padrão: 6633)")
    parser.add_argument("--discovery-wait", type=int, default=8,
                        help="Segundos para descoberta da topologia antes dos testes")
    return parser.parse_args()


def carregar_topologia(arquivo_topo):
    edges = []
    switches = set()
    if not arquivo_topo.is_file():
        raise FileNotFoundError(f"Arquivo de topologia não encontrado: {arquivo_topo}")

    with arquivo_topo.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(
                    f"{arquivo_topo.name}:{line_no}: esperado exatamente dois switches por linha"
                )
            u, v = parts
            if not re.fullmatch(r"s[1-9][0-9]*", u) or not re.fullmatch(r"s[1-9][0-9]*", v):
                raise ValueError(
                    f"{arquivo_topo.name}:{line_no}: nomes devem seguir o padrão s1, s2, ..."
                )
            if u == v:
                raise ValueError(f"{arquivo_topo.name}:{line_no}: enlace de um switch para si mesmo")
            edges.append((u, v))
            switches.update((u, v))

    if not edges or not switches:
        raise ValueError(f"{arquivo_topo.name} não contém enlaces válidos.")

    ids = [int(s[1:]) for s in switches]
    if max(ids) > 254:
        raise ValueError("Esta implementação usa IPs 10.0.0.N; os IDs dos switches devem ser <= 254.")
    # Elimina arestas duplicadas sem perder a ordem de entrada.
    unique_edges = []
    seen = set()
    for u, v in edges:
        key = tuple(sorted((u, v), key=lambda s: int(s[1:])))
        if key not in seen:
            seen.add(key)
            unique_edges.append((u, v))
    return sorted(switches, key=lambda s: int(s[1:])), unique_edges


def controller_reachable(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=1):
            return True
    except OSError:
        return False


def iniciar_ryu_se_necessario(args):
    if controller_reachable(args.controller_ip, args.controller_port):
        info("*** Controlador já responde em %s:%s; usando processo existente.\\n" %
             (args.controller_ip, args.controller_port))
        return None

    ryu_manager = shutil.which("ryu-manager")
    if not ryu_manager:
        raise RuntimeError(
            "Não encontrei 'ryu-manager' no PATH. Instale o Ryu ou inicie o controlador "
            "manualmente antes de executar fctmain.py."
        )
    if not RYU_APP.is_file():
        raise FileNotFoundError(f"Aplicação Ryu não encontrada: {RYU_APP}")

    env = os.environ.copy()
    env["SDN_ROUTING"] = args.routing
    log_file = RYU_LOG.open("a", encoding="utf-8")
    command = [
        ryu_manager, "--observe-links",
        "--ofp-tcp-listen-port", str(args.controller_port),
        str(RYU_APP)
    ]
    info("*** Iniciando Ryu em segundo plano (modo=%s)...\\n" % args.routing)
    proc = subprocess.Popen(
        command, cwd=str(BASE_DIR), env=env,
        stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True
    )
    # O processo mantém o descritor do log aberto.
    deadline = time.time() + 15
    while time.time() < deadline:
        if controller_reachable(args.controller_ip, args.controller_port):
            info("*** Ryu está aceitando conexões OpenFlow. Log: %s\\n" % RYU_LOG)
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    try:
        tail = RYU_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
    except OSError:
        tail = []
    if proc.poll() is None:
        proc.terminate()
    raise RuntimeError(
        "Ryu não iniciou corretamente. Verifique ryu.log.\\n" + "\\n".join(tail)
    )


def desabilitar_ipv6(net):
    info("*** Desabilitando IPv6 nos hosts e switches...\\n")
    for node in net.switches + net.hosts:
        node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
        node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
        node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")


def executar_experimento():
    args = parse_args()
    topo_file = BASE_DIR / ("torus.txt" if args.topology == "torus" else "spineleaf.txt")
    sw_names, edges = carregar_topologia(topo_file)
    log_prefix = f"{args.topology}_{args.routing}_exp{args.exp}"
    bwm_log = BASE_DIR / f"bwm_{log_prefix}.csv"
    fct_log = BASE_DIR / f"fct_{log_prefix}.txt"

    if args.exp == 2:
        failure = ("s4", "s5") if args.topology == "torus" else ("s1", "s3")
        edge_set = {frozenset(edge) for edge in edges}
        if frozenset(failure) not in edge_set:
            raise ValueError(
                f"Experimento 2 requer o enlace {failure[0]}-{failure[1]} em {topo_file.name}, "
                "mas ele não foi encontrado. Ajuste o arquivo ou o enlace de falha no script."
            )

    info("\\n===================================================\\n")
    info(" Topologia=%s | Roteamento=%s | Experimento=%s\\n" %
         (args.topology, args.routing, args.exp))
    info(" Switches: %s\\n Enlaces: %d\\n" % (", ".join(sw_names), len(edges)))
    info("===================================================\\n\\n")

    ryu_proc = None
    net = None
    bwm_proc = None
    try:
        ryu_proc = iniciar_ryu_se_necessario(args)

        net = Mininet(controller=None, switch=OVSKernelSwitch, autoSetMacs=False, build=False)
        net.addController(
            "c0", controller=RemoteController,
            ip=args.controller_ip, port=args.controller_port
        )

        sw_objs = {}
        for s in sw_names:
            sw_objs[s] = net.addSwitch(s, protocols="OpenFlow13")

        host_objs = {}
        for s in sw_names:
            h_id = int(s[1:])
            host = net.addHost(
                f"h{h_id}", ip=f"10.0.0.{h_id}/8",
                mac=f"00:00:00:00:00:{h_id:02x}"
            )
            host_objs[host.name] = host
            net.addLink(host, sw_objs[s])

        for u, v in edges:
            net.addLink(sw_objs[u], sw_objs[v])

        info("*** Iniciando Mininet e conectando switches ao Ryu...\\n")
        net.build()
        net.start()
        desabilitar_ipv6(net)

        info("*** Aguardando %s s para descoberta LLDP/topologia no Ryu...\\n" %
             args.discovery_wait)
        time.sleep(max(1, args.discovery_wait))
        info("*** Teste inicial de conectividade (pingAll)...\\n")
        net.pingAll()

        info("*** Iniciando monitoramento de largura de banda: %s\\n" % bwm_log.name)
        bwm_file = bwm_log.open("w", encoding="utf-8")
        bwm_proc = subprocess.Popen(
            ["bwm-ng", "-t", "1000", "-o", "csv", "-u", "bits", "-T", "rate", "-C", ","],
            stdout=bwm_file, stderr=subprocess.STDOUT, start_new_session=True
        )

        info("*** Iniciando servidores Iperf nos hosts...\\n")
        for h in host_objs.values():
            h.cmd("iperf -s -p 5001 >/tmp/iperf-server-%s.log 2>&1 &" % h.name)
        time.sleep(2)

        info("*** Disparando fluxos Iperf todos-para-todos (5 MB por par)...\\n")
        start_time = time.time()
        for src_name, src_host in host_objs.items():
            for dst_name, dst_host in host_objs.items():
                if src_name != dst_name:
                    src_host.cmd(
                        "iperf -c %s -p 5001 -n 5M >/tmp/iperf-client-%s-%s.log 2>&1 &"
                        % (dst_host.IP(), src_name, dst_name)
                    )

        if args.exp == 2:
            info("*** [Exp 2] Aguardando 15 s antes de derrubar enlace...\\n")
            time.sleep(15)
            u_fail, v_fail = failure
            info("*** [Exp 2] Derrubando enlace %s <-> %s\\n" % (u_fail, v_fail))
            net.configLinkStatus(u_fail, v_fail, "down")

        info("*** Aguardando finalização dos fluxos (limite de 300 s)...\\n")
        deadline = time.time() + 300
        while time.time() < deadline:
            running = False
            for h in host_objs.values():
                if h.cmd("pgrep -f '[i]perf -c'").strip():
                    running = True
                    break
            if not running:
                break
            time.sleep(1)
        else:
            info("*** AVISO: limite de 300 s atingido; encerrando clientes Iperf restantes.\\n")

        end_time = time.time()
        total_fct = end_time - start_time
        info("\\n===================================================\\n")
        info(" FCT TOTAL (%s): %.2f segundos\\n" % (log_prefix, total_fct))
        info("===================================================\\n\\n")

        with fct_log.open("w", encoding="utf-8") as f:
            f.write(
                f"Topology: {args.topology}\\nRouting: {args.routing}\\n"
                f"Exp: {args.exp}\\nFCT_Seconds: {total_fct:.4f}\\n"
            )
        info("*** Resultado FCT gravado em %s\\n" % fct_log)
    finally:
        if net is not None:
            info("*** Encerrando processos Iperf nos hosts e Mininet...\\n")
            for h in net.hosts:
                h.cmd("pkill -f '[i]perf' >/dev/null 2>&1 || true")
            net.stop()
        if bwm_proc is not None and bwm_proc.poll() is None:
            bwm_proc.terminate()
            try:
                bwm_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bwm_proc.kill()
        if ryu_proc is not None and ryu_proc.poll() is None:
            info("*** Encerrando o Ryu iniciado por fctmain.py...\\n")
            try:
                os.killpg(ryu_proc.pid, signal.SIGTERM)
                ryu_proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(ryu_proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    setLogLevel("info")
    try:
        executar_experimento()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
