#!/usr/bin/env python3
"""
Controlador SDN Ryu com Descoberta de Topologia, Roteamento OSPF/ECMP e Tolerância a Falhas.

Arquitetura e funcionalidades principais:
1. Descoberta Dinâmica (Discovery): Constrói e mantém um grafo direcionado (`networkx.DiGraph`)
   representando switches (nós) e enlaces internos (arestas com porta de saída) via eventos LLDP.
2. Algoritmos de Roteamento (selecionados via variável de ambiente `SDN_ROUTING`):
   - `ospf`: Calcula o menor caminho único entre origem e destino (`nx.shortest_path`).
   - `ecmp`: Calcula todos os caminhos mínimos de mesmo custo (`nx.all_shortest_paths`) e
     seleciona uma rota de forma determinística por par de switches usando função de hash.
3. Encaminhamento Proativo por Fluxo: Instala regras OpenFlow 1.3 (prioridade 20) nos switches
   ao longo do caminho para encaminhar quadros unicast diretamente no plano de dados.
4. Flooding Seguro sem Loops: Utiliza uma Árvore Geradora Mínima (`nx.minimum_spanning_tree`)
   para inundar pacotes ARP/Broadcast sem provocar tempestades de broadcast em topologias cíclicas (Torus).
5. Reconvergência Rápida a Falhas: Monitora `EventOFPPortStatus` (`OFPPS_LINK_DOWN`) e `EventLinkDelete`,
   remove imediatamente as arestas afetadas do grafo, limpa as regras antigas de todos os switches,
   restaura a regra `table-miss` (prioridade 0) e registra no log a nova tabela de rotas calculada.
"""

import os
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4
from ryu.topology import event
from ryu.topology.api import get_switch, get_link


class TopoDiscover(app_manager.RyuApp):
    """Aplicação principal do controlador Ryu usando OpenFlow 1.3."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        """Inicializa estruturas de dados da topologia, tabelas de hosts e modo de roteamento."""
        super(TopoDiscover, self).__init__(*args, **kwargs)

        self.topology_api_app = self
        self.routing = os.environ.get("SDN_ROUTING", "ospf").lower()

        if self.routing not in ("ospf", "ecmp"):
            self.logger.warning("Modo %s inválido; usando ospf", self.routing)
            self.routing = "ospf"

        # Estruturas de estado da rede
        self.net = nx.DiGraph()  # Grafo direcionado: nós = DPIDs; arestas = (u, v, port=porta_saida_u)
        self.switches = {}       # Mapeamento DPID (int) -> objeto Datapath
        self.hosts = {}          # Mapeamento IP (str) -> {'mac': str, 'dpid': int, 'port': int}
        self.mac_to_host = {}    # Mapeamento reverso MAC (str) -> IP (str)

        self.logger.info(
            "Controlador iniciado; roteamento=%s", self.routing
        )

    # ==========================================================================
    # 1. CONEXÃO DE SWITCHES E REGRA TABLE-MISS
    # ==========================================================================

    def _install_table_miss(self, dp):
        """
        Instala a regra padrão (prioridade 0) no switch.
        Qualquer pacote que não corresponda a uma regra específica é enviado ao controlador (PacketIn).
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER,
                ofp.OFPCML_NO_BUFFER
            )
        ]
        self._add_flow(dp, 0, match, actions)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Trata o handshake inicial de cada switch OpenFlow conectado ao controlador."""
        dp = ev.msg.datapath
        self.switches[dp.id] = dp

        self._install_table_miss(dp)

        self.logger.info(
            "Switch conectado: s%s; table-miss instalado", dp.id
        )
        self._refresh_topology()

    # ==========================================================================
    # 2. DESCOBERTA DE TOPOLOGIA (LLDP / NETWORKX)
    # ==========================================================================

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        """Disparado quando um novo switch entra na topologia."""
        self._refresh_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        """Disparado quando um switch deixa a topologia."""
        self._refresh_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Disparado quando um enlace entre switches é descoberto via LLDP."""
        self._refresh_topology()

    def _refresh_topology(self):
        """
        Consulta a API de topologia do Ryu (`get_switch` e `get_link`) e reconstrói
        o grafo direcionado `self.net` no NetworkX com os switches e portas atuais.
        """
        try:
            switch_list = get_switch(self.topology_api_app, None)
            link_list = get_link(self.topology_api_app, None)

            self.switches.update({sw.dp.id: sw.dp for sw in switch_list})

            self.net.clear()
            for sw in switch_list:
                self.net.add_node(sw.dp.id)

            for link in link_list:
                src_dpid = link.src.dpid
                dst_dpid = link.dst.dpid
                src_port = link.src.port_no
                self.net.add_edge(src_dpid, dst_dpid, port=src_port)

            self.logger.info(
                "Discovery (NetworkX): %d nós, %d enlaces direcionados",
                self.net.number_of_nodes(),
                self.net.number_of_edges(),
            )

            self.print_full_topology()
            self.log_routing_table(contexto="ANTES DA FALHA / TOPOLOGIA ATUAL")

        except Exception as exc:
            self.logger.debug("Topologia ainda não disponível: %s", exc)

    def print_full_topology(self):
        """Registra no log os nós e enlaces direcionados atualmente conhecidos."""
        self.logger.info("Switches no grafo: %s", sorted(list(self.net.nodes)))
        for u, v, data in sorted(self.net.edges(data=True)):
            self.logger.info("Enlace: s%s -> s%s (porta %s)", u, v, data.get("port"))

    # ==========================================================================
    # 3. ALGORITMOS DE ROTEAMENTO (OSPF E ECMP) E LOG DE ROTAS
    # ==========================================================================

    def get_route_ecmp(self, switch_in: str, switch_out: str, paths: list):
        """
        Seleciona deterministicamente um dos caminhos mínimos de mesmo custo (ECMP)
        aplicando uma função de hash sobre os identificadores dos switches de origem e destino.

        Parâmetros:
            switch_in (str): Identificador do switch de origem.
            switch_out (str): Identificador do switch de destino.
            paths (list[list[int]]): Lista de todos os caminhos mínimos encontrados.

        Retorna:
            list[int] | None: O caminho escolhido ou None caso a lista esteja vazia.
        """
        if paths:
            hash_val = hash(str(switch_in) + str(switch_out))
            choice = hash_val % len(paths)
            path = sorted(paths)[choice]
            return path
        return None

    def find_route(self, src_dpid: int, dst_dpid: int, method: str):
        """
        Calcula a sequência de switches (lista de DPIDs) entre `src_dpid` e `dst_dpid`.

        Parâmetros:
            src_dpid (int): DPID do switch atual/origem.
            dst_dpid (int): DPID do switch onde o host destino está conectado.
            method (str): 'ospf' para menor caminho único ou 'ecmp' para múltiplos caminhos mínimos.

        Retorna:
            list[int] | None: Lista ordenada de DPIDs representando a rota, ou None se inacessível.
        """
        if not self.net.has_node(src_dpid) or not self.net.has_node(dst_dpid):
            return None

        try:
            if method == "ospf":
                return nx.shortest_path(self.net, source=src_dpid, target=dst_dpid)
            elif method == "ecmp":
                all_paths = list(nx.all_shortest_paths(self.net, source=src_dpid, target=dst_dpid))
                return self.get_route_ecmp(str(src_dpid), str(dst_dpid), all_paths)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            self.logger.warning("Caminho não encontrado entre s%s e s%s", src_dpid, dst_dpid)
            return None

        return None

    def log_routing_table(self, contexto: str = "ATUAL"):
        """
        Percorre todos os switches do grafo (`self.net`) e registra no log
        a rota calculada e a porta de saída para cada switch de destino.
        """
        nodes = sorted(self.net.nodes())
        if len(nodes) < 2:
            return

        self.logger.info("==========================================================")
        self.logger.info(" TABELA DE ROTAS (%s) | Modo: %s", contexto, self.routing.upper())
        self.logger.info("==========================================================")

        for src in nodes:
            rotas_sw = []
            for dst in nodes:
                if src == dst:
                    continue
                path = self.find_route(src, dst, self.routing)
                if path and len(path) >= 2:
                    next_hop = path[1]
                    out_port = self.net[src][next_hop].get("port")
                    caminho_str = " -> ".join(f"s{n}" for n in path)
                    rotas_sw.append(f"  para s{dst}: [{caminho_str}] (sai pela porta {out_port})")
                else:
                    rotas_sw.append(f"  para s{dst}: [SEM ROTA]")

            self.logger.info("Switch s%s:\n%s", src, "\n".join(rotas_sw))

        self.logger.info("==========================================================")

    # ==========================================================================
    # 4. GERENCIAMENTO DE REGRAS OPENFLOW (FLOWMOD)
    # ==========================================================================

    def _add_flow(self, dp, priority: int, match, actions: list, buffer_id=None):
        """Envia uma mensagem OFPFlowMod para instalar uma regra de encaminhamento no switch."""
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        instructions = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        kwargs = {
            "datapath": dp,
            "priority": priority,
            "match": match,
            "instructions": instructions,
        }

        if buffer_id is not None and buffer_id != ofp.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id

        dp.send_msg(parser.OFPFlowMod(**kwargs))

    def _clear_all_flows_and_restore_table_miss(self):
        """
        Remove todas as regras antigas de TODOS os switches conectados (`OFPFC_DELETE`)
        e reinstala imediatamente a regra `table-miss` (prioridade 0) em cada um deles.
        Garante que nenhum pacote fique preso em rotas obsoletas após uma falha de enlace.
        """
        for sw_dp in list(self.switches.values()):
            parser = sw_dp.ofproto_parser
            ofp = sw_dp.ofproto
            flow_mod = parser.OFPFlowMod(
                datapath=sw_dp,
                table_id=ofp.OFPTT_ALL,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(),
            )
            sw_dp.send_msg(flow_mod)
            self._install_table_miss(sw_dp)

    # ==========================================================================
    # 5. APRENDIZADO DE HOSTS (EDGE PORTS)
    # ==========================================================================

    def _switch_link_ports(self) -> set:
        """Retorna o conjunto de pares `(dpid, port_no)` usados em enlaces internos entre switches."""
        ports = set()
        for u, v, data in self.net.edges(data=True):
            ports.add((u, data.get("port")))
        return ports

    def _learn_host(self, ip: str, mac: str, dpid: int, port: int):
        """
        Associa o endereço IP e MAC de um host à porta de borda do switch onde ele está conectado.
        Ignora portas internas entre switches para não sobrescrever a localização real do host.
        """
        if not ip or ip == "0.0.0.0":
            return

        if (dpid, port) in self._switch_link_ports():
            return

        old = self.hosts.get(ip)

        self.hosts[ip] = {
            "mac": mac,
            "dpid": dpid,
            "port": port,
        }
        self.mac_to_host[mac] = ip

        if old != self.hosts[ip]:
            self.logger.info(
                "Host aprendido: IP=%s MAC=%s switch=s%s porta=%s",
                ip, mac, dpid, port,
            )

    # ==========================================================================
    # 6. ENCAMINHAMENTO UNICAST E FLOODING SEGURO (SPANNING TREE)
    # ==========================================================================

    def _route_actions(self, dpid: int, dst_ip: str, dst_host: dict) -> list:
        """
        Determina a ação de saída para um pacote unicast destinado a um host conhecido
        e instala a regra correspondente (prioridade 20) na tabela de fluxos do switch atual.
        """
        dp = self.switches.get(dpid)
        if dp is None:
            return []

        parser = dp.ofproto_parser

        # Caso 1: O host de destino está diretamente conectado a este switch
        if dpid == dst_host["dpid"]:
            actions = [parser.OFPActionOutput(dst_host["port"])]
            match = parser.OFPMatch(eth_dst=dst_host["mac"])
            self._add_flow(dp, 20, match, actions)
            return actions

        # Caso 2: Calcula a rota até o switch de destino e encaminha para o próximo salto (next-hop)
        path = self.find_route(dpid, dst_host["dpid"], self.routing)
        if not path or len(path) < 2:
            return []

        next_hop = path[1]
        out_port = self.net[dpid][next_hop]["port"]

        actions = [parser.OFPActionOutput(out_port)]
        match = parser.OFPMatch(eth_dst=dst_host["mac"])
        self._add_flow(dp, 20, match, actions)

        return actions

    def _flood_actions(self, dpid: int, in_port: int) -> list:
        """
        Calcula as portas de saída para inundação (flooding) de quadros ARP ou destino desconhecido.
        Restringe o envio entre switches apenas às arestas pertencentes a uma Árvore Geradora Mínima
        (Minimum Spanning Tree), prevenindo loops infinitos em topologias com ciclos (como Torus).
        """
        dp = self.switches.get(dpid)
        if dp is None or not self.net.nodes:
            return []

        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # Constrói a Árvore Geradora Mínima sobre o grafo não direcionado
        undirected_net = self.net.to_undirected()
        st_edges = set()
        if len(undirected_net) > 0:
            st = nx.minimum_spanning_tree(undirected_net)
            for u, v in st.edges():
                st_edges.add(tuple(sorted((u, v))))

        actions = []
        try:
            switch_list = get_switch(self.topology_api_app, None)
            sw = next((item for item in switch_list if item.dp.id == dpid), None)
            ports = [p.port_no for p in sw.ports] if sw else []
        except Exception:
            ports = []

        for port in sorted(set(ports)):
            if port in (in_port, ofp.OFPP_LOCAL, ofp.OFPP_CONTROLLER):
                continue

            # Verifica se a porta conecta este switch a outro switch
            is_link_port = False
            is_st_port = False
            for u, v, data in self.net.edges(data=True):
                if u == dpid and data.get("port") == port:
                    is_link_port = True
                    if tuple(sorted((u, v))) in st_edges:
                        is_st_port = True
                    break

            # Bloqueia portas entre switches que não fazem parte da Spanning Tree
            if is_link_port and not is_st_port:
                continue

            actions.append(parser.OFPActionOutput(port))

        return actions

    # ==========================================================================
    # 7. TRATAMENTO DE PACOTES RECEBIDOS (PACKET-IN)
    # ==========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Processa pacotes enviados ao controlador pela regra table-miss:
        1. Ignora quadros LLDP usados internamente pela descoberta de topologia.
        2. Extrai o IP/MAC de origem (via ARP ou IPv4) para registrar a localização do host.
        3. Encaminha via rota unicast (se destino conhecido) ou via flooding seguro (Spanning Tree).
        """
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        # Ignora pacotes LLDP do próprio Ryu
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_ip = None
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        if arp_pkt:
            src_ip = arp_pkt.src_ip
        elif ip_pkt:
            src_ip = ip_pkt.src

        # Atualiza o registro do host remetente
        self._learn_host(src_ip, eth.src, dpid, in_port)

        dst_ip = self.mac_to_host.get(eth.dst)
        dst_host = self.hosts.get(dst_ip) if dst_ip else None

        if dst_host and eth.dst != "ff:ff:ff:ff:ff:ff":
            actions = self._route_actions(dpid, dst_ip, dst_host)
        else:
            actions = self._flood_actions(dpid, in_port)

        if not actions:
            return

        data = None if msg.buffer_id != dp.ofproto.OFP_NO_BUFFER else msg.data

        out = dp.ofproto_parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        dp.send_msg(out)

    # ==========================================================================
    # 8. TRATAMENTO DINÂMICO DE FALHAS E RECONVERGÊNCIA
    # ==========================================================================

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        """
        Trata a remoção de um enlace detectada pelo módulo de topologia LLDP,
        removendo a aresta do grafo, limpando as tabelas de fluxo e registrando as novas rotas.
        """
        src = ev.link.src
        dst = ev.link.dst
        self.logger.warning(
            "EventLinkDelete: enlace s%s (porta %s) -> s%s (porta %s) removido",
            src.dpid, src.port_no, dst.dpid, dst.port_no
        )
        if self.net.has_edge(src.dpid, dst.dpid):
            self.net.remove_edge(src.dpid, dst.dpid)
        self._clear_all_flows_and_restore_table_miss()
        self.log_routing_table(contexto=f"APÓS EventLinkDelete s{src.dpid} -> s{dst.dpid}")

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """
        Detecta imediatamente a queda física/administrativa de uma porta (`OFPPS_LINK_DOWN`),
        remove ambas as direções do enlace afetado no grafo NetworkX, força o recálculo
        global das rotas em todos os switches e registra a nova tabela de rotas no log.
        """
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port_no = msg.desc.port_no

        if msg.reason in (ofp.OFPPR_DELETE, ofp.OFPPR_MODIFY):
            if msg.desc.state & ofp.OFPPS_LINK_DOWN:
                self.logger.warning(
                    "FALHA DETECTADA: Switch s%s - Porta %s caiu (Link Down)!",
                    dp.id, port_no
                )
                # Remove imediatamente do grafo as duas direções da aresta vinculada à porta caída
                for u, v, data in list(self.net.edges(data=True)):
                    if u == dp.id and data.get("port") == port_no:
                        self.net.remove_edge(u, v)
                        if self.net.has_edge(v, u):
                            self.net.remove_edge(v, u)

                # Limpa regras antigas em todos os switches e restaura o table-miss (prioridade 0)
                self._clear_all_flows_and_restore_table_miss()
                self.logger.info(
                    "Regras antigas limpas e table-miss restaurado em TODOS os switches."
                )
                self.log_routing_table(contexto=f"APÓS QUEDA NA PORTA {port_no} DE s{dp.id}")
