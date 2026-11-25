#!/usr/bin/env python3
"""
Orquestrador de Experimentos SDN com Mininet, Controlador Ryu e Análise de Desempenho.

Estrutura de diretórios gerenciada automaticamente pelo script:
- Raiz (`./`): Apenas os scripts Python (`fctmain.py`, `ryu_controller.py`, `plot_fct.py`).
- `topologias/`: Arquivos de definição de enlaces (`torus.txt` e `spineleaf.txt`).
- `graficos/`: Imagens geradas (`vazao_<topologia>_<roteamento>_exp<N>.png`).
- `logs/`: Logs do controlador Ryu (`ryu.log`) e resultados de FCT (`fct_<topologia>_<roteamento>_exp<N>.txt`).
- `registros/`: Coletas brutas de telemetria de banda (`bwm_<topologia>_<roteamento>_exp<N>.csv`).
"""

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

import matplotlib
matplotlib.use("Agg")  # Garante renderização de gráficos em ambientes sem servidor gráfico (headless/SSH)
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info


# ==============================================================================
# CONSTANTES E ESTRUTURA DE DIRETÓRIOS
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
TOPOLOGIAS_DIR = BASE_DIR / "topologias"
GRAFICOS_DIR = BASE_DIR / "graficos"
LOGS_DIR = BASE_DIR / "logs"
REGISTROS_DIR = BASE_DIR / "registros"

RYU_APP = BASE_DIR / "ryu_controller.py"
RYU_LOG = LOGS_DIR / "ryu.log"


def garantir_diretorios():
    """Cria os subdiretórios de organização caso ainda não existam."""
    for pasta in (TOPOLOGIAS_DIR, GRAFICOS_DIR, LOGS_DIR, REGISTROS_DIR):
        pasta.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 1. PARSE DE ARGUMENTOS E VALIDAÇÃO DE ENTRADA
# ==============================================================================

def parse_args():
    """
    Analisa e valida os parâmetros fornecidos via linha de comando (CLI).

    Retorna:
        argparse.Namespace: Objeto contendo todas as configurações do experimento.
    """
    parser = argparse.ArgumentParser(
        description="Experimento SDN: descoberta, encaminhamento e medição FCT (todos-para-todos)"
    )
    parser.add_argument(
        "--topology", choices=["torus", "spineleaf"], required=True,
        help="Topologia de rede definida em topologias/torus.txt ou topologias/spineleaf.txt"
    )
    parser.add_argument(
        "--routing", choices=["ospf", "ecmp"], required=True,
        help="Algoritmo de roteamento: ospf (menor caminho) ou ecmp (múltiplos caminhos via hash)"
    )
    parser.add_argument(
        "--exp", type=int, choices=[1, 2], required=True,
        help="Cenário de teste: 1 = operação normal; 2 = injeta falha de enlace durante o tráfego"
    )
    parser.add_argument(
        "--controller-ip", default="127.0.0.1",
        help="Endereço IP do controlador Ryu (padrão: 127.0.0.1)"
    )
    parser.add_argument(
        "--controller-port", type=int, default=6633,
        help="Porta TCP OpenFlow do controlador Ryu (padrão: 6633)"
    )
    parser.add_argument(
        "--discovery-wait", type=int, default=8,
        help="Tempo de espera (em segundos) para descoberta LLDP da topologia antes dos testes"
    )
    parser.add_argument(
        "--bw", type=int, default=200,
        help="Capacidade de largura de banda de cada enlace em Mbps (padrão: 200)"
    )
    parser.add_argument(
        "--size", default="60M",
        help="Volume de dados transmitido por cada par origem-destino no iperf (padrão: 60M)"
    )
    parser.add_argument(
        "--fail-delay", type=int, default=8,
        help="Tempo (em segundos) de tráfego ativo antes de derrubar o enlace no Exp 2 (padrão: 8)"
    )
    return parser.parse_args()


def carregar_topologia(arquivo_topo: Path):
    """
    Lê um arquivo de definição de topologia a partir da pasta `topologias/` e extrai
    os switches e enlaces únicos.

    Cada linha válida do arquivo deve conter dois identificadores de switch no formato `sN`
    (ex: `s1 s2`). Linhas vazias e comentários iniciados com `#` são ignorados.

    Parâmetros:
        arquivo_topo (Path): Caminho para o arquivo `.txt` da topologia.

    Retorna:
        tuple[list[str], list[tuple[str, str]]]: Lista ordenada de switches e lista de enlaces únicos.
    """
    edges = []
    switches = set()
    if not arquivo_topo.is_file():
        raise FileNotFoundError(
            f"Arquivo de topologia não encontrado: {arquivo_topo}\n"
            f"Certifique-se de mover '{arquivo_topo.name}' para a pasta '{TOPOLOGIAS_DIR}'."
        )

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

    # Garante compatibilidade com o esquema de endereçamento IPv4 10.0.0.N/8
    ids = [int(s[1:]) for s in switches]
    if max(ids) > 254:
        raise ValueError("Esta implementação usa IPs 10.0.0.N; os IDs dos switches devem ser <= 254.")

    # Remove enlaces duplicados não direcionados (ex: 's1 s2' e 's2 s1')
    unique_edges = []
    seen = set()
    for u, v in edges:
        key = tuple(sorted((u, v), key=lambda s: int(s[1:])))
        if key not in seen:
            seen.add(key)
            unique_edges.append((u, v))

    return sorted(switches, key=lambda s: int(s[1:])), unique_edges


# ==============================================================================
# 2. GERENCIAMENTO DO CONTROLADOR RYU
# ==============================================================================

def controller_reachable(ip: str, port: int) -> bool:
    """Verifica se há um processo escutando na porta OpenFlow especificada."""
    try:
        with socket.create_connection((ip, port), timeout=1):
            return True
    except OSError:
        return False


def encerrar_ryu_existente():
    """
    Finaliza qualquer processo `ryu-manager` remanescente em execução.
    Evita que experimentos subsequentes herdem estados ou algoritmos de roteamento anteriores.
    """
    subprocess.run(
        ["pkill", "-9", "-f", "ryu-manager"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.5)


def iniciar_ryu_limpo(args) -> subprocess.Popen:
    """
    Inicia uma nova instância do controlador Ryu em segundo plano com o algoritmo escolhido,
    gravando a saída em `logs/ryu.log`.

    Parâmetros:
        args (argparse.Namespace): Argumentos da linha de comando.

    Retorna:
        subprocess.Popen: Handle do processo do controlador Ryu iniciado.
    """
    if controller_reachable(args.controller_ip, args.controller_port):
        info("*** Encerrando instância antiga do Ryu na porta %s para aplicar modo=%s...\n" %
             (args.controller_port, args.routing))
        encerrar_ryu_existente()

    ryu_manager = shutil.which("ryu-manager")
    if not ryu_manager:
        raise RuntimeError(
            "Não encontrei 'ryu-manager' no PATH. Instale o Ryu antes de executar fctmain.py."
        )
    if not RYU_APP.is_file():
        raise FileNotFoundError(f"Aplicação Ryu não encontrada: {RYU_APP}")

    env = os.environ.copy()
    env["SDN_ROUTING"] = args.routing

    # Sobrescreve logs/ryu.log a cada execução para manter os registros isolados
    log_file = RYU_LOG.open("w", encoding="utf-8")
    command = [
        ryu_manager,
        "--observe-links",  # Habilita o módulo LLDP do Ryu para descoberta dinâmica de enlaces
        "--ofp-tcp-listen-port", str(args.controller_port),
        str(RYU_APP)
    ]

    info("*** Iniciando Ryu (%s) em segundo plano (modo=%s)...\n" % (RYU_APP.name, args.routing))
    proc = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True
    )

    # Aguarda o controlador aceitar conexões TCP na porta OpenFlow
    deadline = time.time() + 15
    while time.time() < deadline:
        if controller_reachable(args.controller_ip, args.controller_port):
            info("*** Ryu aceitando conexões OpenFlow. Log: %s\n" % RYU_LOG)
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    # Caso falhe ao iniciar, exibe as últimas linhas do log para diagnóstico
    try:
        tail = RYU_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
    except OSError:
        tail = []
    if proc.poll() is None:
        proc.terminate()
    raise RuntimeError(
        f"Ryu não iniciou corretamente. Verifique {RYU_LOG}.\n" + "\n".join(tail)
    )


# ==============================================================================
# 3. CONFIGURAÇÃO AUXILIAR DE REDE E GERAÇÃO DE GRÁFICOS
# ==============================================================================

def desabilitar_ipv6(net: Mininet):
    """
    Desativa a pilha IPv6 em todos os switches e hosts virtuais do Mininet.
    Impede o envio de pacotes ICMPv6 (Router/Neighbor Solicitation) que poluem o PacketIn e os gráficos.
    """
    info("*** Desabilitando IPv6 nos hosts e switches...\n")
    for node in net.switches + net.hosts:
        node.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1")
        node.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1")
        node.cmd("sysctl -w net.ipv6.conf.lo.disable_ipv6=1")


def gerar_grafico_vazao(bwm_csv_path: Path, output_png_path: Path, topology: str, routing: str, exp: int, tempo_falha=None):
    """
    Lê o arquivo CSV em `registros/`, agrega a vazão por switch ao longo do tempo
    e salva o gráfico PNG correspondente na pasta `graficos/`.
    """
    if not bwm_csv_path.is_file() or bwm_csv_path.stat().st_size == 0:
        info("*** AVISO: Arquivo CSV de banda vazio ou inexistente; gráfico não gerado.\n")
        return

    df = pd.read_csv(bwm_csv_path, header=None)

    # Mantém apenas interfaces de switches (ignora 'lo', 'eth0' e linhas agregadas 'total')
    df_switches = df[df[1].astype(str).str.startswith("s", na=False)].copy()
    if df_switches.empty:
        info("*** AVISO: Nenhuma interface de switch encontrada no CSV; gráfico não gerado.\n")
        return

    # Extrai o identificador do switch (ex: 's4-eth2' -> 's4')
    df_switches["switch"] = df_switches[1].apply(lambda x: x.split("-")[0])

    # Converte timestamp e vazão para tipos numéricos
    df_switches[0] = pd.to_numeric(df_switches[0], errors="coerce")
    df_switches = df_switches.dropna(subset=[0])
    df_switches["vazao_mbps"] = pd.to_numeric(df_switches[2], errors="coerce") / 1e6

    # Filtra outliers causados por reinicialização/wrap-around de contadores de 64 bits do kernel
    df_switches = df_switches[
        (df_switches["vazao_mbps"] >= 0) & (df_switches["vazao_mbps"] < 100000)
    ].copy()

    if df_switches.empty:
        info("*** AVISO: Nenhuma amostra válida no CSV; gráfico não gerado.\n")
        return

    # Normaliza o tempo para iniciar em t = 0s e arredonda para o intervalo de 1s do bwm-ng
    t0 = df_switches[0].min()
    df_switches["tempo_relativo"] = df_switches[0] - t0
    df_switches["tempo_bin"] = np.round(df_switches["tempo_relativo"])

    # Agrega a vazão total de todas as interfaces de cada switch por segundo
    df_grouped = df_switches.groupby(["tempo_bin", "switch"])["vazao_mbps"].sum().unstack().fillna(0)
    sorted_cols = sorted(df_grouped.columns, key=lambda s: int(s[1:]) if s[1:].isdigit() else s)
    df_grouped = df_grouped[sorted_cols]

    # Renderização do gráfico de linhas
    plt.figure(figsize=(12, 6))
    for switch_name in df_grouped.columns:
        plt.plot(df_grouped.index, df_grouped[switch_name], label=switch_name, linewidth=2)

    # Adiciona linha vertical de referência no instante em que o enlace foi derrubado (Exp 2)
    if exp == 2 and tempo_falha is not None:
        plt.axvline(
            x=tempo_falha, color="red", linestyle="--", linewidth=1.5,
            label=f"Queda do Enlace ({tempo_falha}s)"
        )

    plt.title(f"Vazão vs. Tempo - Distribuição de Carga por Switch ({topology.upper()} - {routing.upper()} E{exp})")
    plt.xlabel("Tempo (segundos)")
    plt.ylabel("Vazão Total (Mbps)")
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.legend(title="Switches", bbox_to_anchor=(1.05, 1), loc="upper left")
    plt.tight_layout()

    plt.savefig(output_png_path, dpi=300)
    plt.close()
    info("*** Gráfico gerado com sucesso e salvo em: %s\n" % output_png_path)


# ==============================================================================
# 4. ORQUESTRAÇÃO PRINCIPAL DO EXPERIMENTO
# ==============================================================================

def executar_experimento():
    """
    Executa o fluxo completo do experimento SDN mantendo a estrutura de pastas organizada:
    - Lê topologias de `topologias/`
    - Salva CSVs em `registros/`
    - Salva `.txt` e `.log` em `logs/`
    - Salva `.png` em `graficos/`
    """
    garantir_diretorios()
    args = parse_args()

    topo_file = TOPOLOGIAS_DIR / ("torus.txt" if args.topology == "torus" else "spineleaf.txt")
    sw_names, edges = carregar_topologia(topo_file)

    # Define nomes e pastas padronizadas dos arquivos de saída para esta combinação
    log_prefix = f"{args.topology}_{args.routing}_exp{args.exp}"
    bwm_log = REGISTROS_DIR / f"bwm_{log_prefix}.csv"
    fct_log = LOGS_DIR / f"fct_{log_prefix}.txt"
    plot_png = GRAFICOS_DIR / f"vazao_{log_prefix}.png"

    # Valida previamente se o enlace alvo da falha existe na topologia escolhida
    if args.exp == 2:
        failure = ("s4", "s5") if args.topology == "torus" else ("s1", "s3")
        edge_set = {frozenset(edge) for edge in edges}
        if frozenset(failure) not in edge_set:
            raise ValueError(
                f"Experimento 2 requer o enlace {failure[0]}-{failure[1]} em {topo_file}, "
                "mas ele não foi encontrado. Ajuste o arquivo ou o enlace de falha no script."
            )

    info("\n===================================================\n")
    info(" Topologia=%s | Roteamento=%s | Experimento=%s | Banda=%d Mbps | Fluxo=%s\n" %
         (args.topology, args.routing, args.exp, args.bw, args.size))
    info(" Switches: %s\n Enlaces: %d\n" % (", ".join(sw_names), len(edges)))
    info("===================================================\n\n")

    ryu_proc = None
    net = None
    bwm_proc = None
    bwm_file = None
    tempo_falha = None

    try:
        # Etapa 1: Inicialização do controlador Ryu
        ryu_proc = iniciar_ryu_limpo(args)

        # Etapa 2: Construção da rede Mininet com OpenFlow 1.3 e enlaces TCLink
        net = Mininet(controller=None, switch=OVSKernelSwitch, autoSetMacs=False, build=False)
        net.addController(
            "c0", controller=RemoteController,
            ip=args.controller_ip, port=args.controller_port
        )

        sw_objs = {}
        for s in sw_names:
            sw_objs[s] = net.addSwitch(s, protocols="OpenFlow13")

        # Cria um host hN (IP 10.0.0.N, MAC 00:00:00:00:00:NN) conectado a cada switch sN
        host_objs = {}
        for s in sw_names:
            h_id = int(s[1:])
            host = net.addHost(
                f"h{h_id}",
                ip=f"10.0.0.{h_id}/8",
                mac=f"00:00:00:00:00:{h_id:02x}"
            )
            host_objs[host.name] = host
            net.addLink(host, sw_objs[s], cls=TCLink, bw=args.bw)

        # Cria os enlaces internos entre os switches conforme a topologia
        for u, v in edges:
            net.addLink(sw_objs[u], sw_objs[v], cls=TCLink, bw=args.bw)

        info("*** Iniciando Mininet e conectando switches ao Ryu...\n")
        net.build()
        net.start()
        desabilitar_ipv6(net)

        # Etapa 3: Descoberta da topologia (LLDP) e validação inicial de alcançabilidade
        info("*** Aguardando %s s para descoberta LLDP/topologia no Ryu...\n" %
             args.discovery_wait)
        time.sleep(max(1, args.discovery_wait))
        info("*** Teste inicial de conectividade (pingAll)...\n")
        net.pingAll()

        # Etapa 4: Início da telemetria de banda (bwm-ng) e servidores Iperf
        info("*** Iniciando monitoramento de largura de banda: %s\n" % bwm_log)
        bwm_file = bwm_log.open("w", encoding="utf-8")
        bwm_proc = subprocess.Popen(
            ["bwm-ng", "-t", "1000", "-o", "csv", "-u", "bits", "-T", "rate", "-C", ","],
            stdout=bwm_file, stderr=subprocess.STDOUT, start_new_session=True
        )

        info("*** Iniciando servidores Iperf nos hosts...\n")
        for h in host_objs.values():
            h.cmd("iperf -s -p 5001 >/tmp/iperf-server-%s.log 2>&1 &" % h.name)
        time.sleep(2)

        # Etapa 5: Disparo dos fluxos TCP todos-para-todos (N * (N - 1) pares simultâneos)
        total_pares = len(host_objs) * (len(host_objs) - 1)
        info("*** Disparando %d fluxos Iperf todos-para-todos (%s por par)...\n" %
             (total_pares, args.size))
        start_time = time.time()
        for src_name, src_host in host_objs.items():
            for dst_name, dst_host in host_objs.items():
                if src_name != dst_name:
                    # src_host.cmd(
                    #     "iperf -c %s -p 5001 -n %s >/tmp/iperf-client-%s-%s.log 2>&1 &"
                    #     % (dst_host.IP(), args.size, src_name, dst_name)
                    # )
                    src_host.cmd(
                        "iperf -c %s -p 5001 -P 4 -n %s >/tmp/iperf-client-%s-%s.log 2>&1 &"
                        % (dst_host.IP(), args.size, src_name, dst_name)
                    )

        # Etapa 6: Injeção de falha no meio da execução (apenas no Experimento 2)
        if args.exp == 2:
            info("*** [Exp 2] Aguardando %d s de transferência antes de derrubar enlace...\n" % args.fail_delay)
            time.sleep(args.fail_delay)
            u_fail, v_fail = failure
            tempo_falha = round(time.time() - start_time)
            info("*** [Exp 2] Derrubando enlace %s <-> %s no meio do teste (t=%ds)...\n" %
                 (u_fail, v_fail, tempo_falha))
            net.configLinkStatus(u_fail, v_fail, "down")

        # Etapa 7: Aguarda a conclusão de todos os clientes Iperf para calcular o FCT total
        info("*** Aguardando conclusão de todos os fluxos Iperf (limite de 300 s)...\n")
        deadline = time.time() + 300
        while time.time() < deadline:
            res = subprocess.run(["pgrep", "-fc", "[i]perf -c"], capture_output=True, text=True)
            ativos = int(res.stdout.strip() or 0)
            if ativos == 0:
                break
            time.sleep(1)
        else:
            info("*** AVISO: limite de 300 s atingido; encerrando clientes Iperf restantes.\n")
            subprocess.run(["pkill", "-9", "-f", "[i]perf -c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        end_time = time.time()
        total_fct = end_time - start_time

        info("\n===================================================\n")
        info(" FCT TOTAL (%s): %.2f segundos\n" % (log_prefix, total_fct))
        info("===================================================\n\n")

        # Salva o resultado consolidado do FCT na pasta logs/
        with fct_log.open("w", encoding="utf-8") as f:
            f.write(
                f"Topology: {args.topology}\nRouting: {args.routing}\n"
                f"Exp: {args.exp}\nFCT_Seconds: {total_fct:.4f}\n"
            )
        info("*** Resultado FCT gravado em: %s\n" % fct_log)

    finally:
        # Etapa 8: Limpeza garantida de processos, interfaces virtuais e descritores de arquivo
        print()
        if net is not None:
            info("*** Encerrando processos Iperf nos hosts e Mininet...\n")
            for h in net.hosts:
                h.cmd("pkill -f '[i]perf' >/dev/null 2>&1 || true")
            net.stop()
        if bwm_proc is not None and bwm_proc.poll() is None:
            bwm_proc.terminate()
            try:
                bwm_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bwm_proc.kill()
        if bwm_file is not None and not bwm_file.closed:
            bwm_file.flush()
            bwm_file.close()
        if ryu_proc is not None and ryu_proc.poll() is None:
            info("*** Encerrando o Ryu iniciado por fctmain.py...\n")
            try:
                os.killpg(ryu_proc.pid, signal.SIGTERM)
                ryu_proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(ryu_proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    # Etapa 9: Geração do gráfico de distribuição de vazão na pasta graficos/
    gerar_grafico_vazao(bwm_log, plot_png, args.topology, args.routing, args.exp, tempo_falha)


if __name__ == "__main__":
    setLogLevel("info")
    try:
        executar_experimento()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
