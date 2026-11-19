#!/usr/bin/env python3
"""
Controlador Ryu para descoberta de topologia via NetworkX e encaminhamento OpenFlow 1.3.

- ospf: encaminhamento por menor caminho (NetworkX shortest_path).
- ecmp: múltiplos menores caminhos com seleção via função de hash determinística (get_route_ecmp).
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
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TopoDiscover, self).__init__(*args, **kwargs)

        self.topology_api_app = self
        self.routing = os.environ.get("SDN_ROUTING", "ospf").lower()

        if self.routing not in ("ospf", "ecmp"):
            self.logger.warning("Modo %s inválido; usando ospf", self.routing)
            self.routing = "ospf"

        # Instancia o Grafo Direcionado do NetworkX
        self.net = nx.DiGraph()

        self.switches = {}       # DPID -> datapath
        self.hosts = {}          # IP -> {mac, dpid, port}
        self.mac_to_host = {}    # MAC -> IP

        self.logger.info(
            "Controlador iniciado; roteamento=%s", self.routing
        )

    # ---------------------------------------------------------
    # Conexão dos switches e regra table-miss
    # ---------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.switches[dp.id] = dp

        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # Regra Table-Miss: Envia pacotes não casados para o controlador
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofp.OFPP_CONTROLLER,
                ofp.OFPCML_NO_BUFFER
            )
        ]

        self._add_flow(dp, 0, match, actions)

        self.logger.info(
            "Switch conectado: s%s; table-miss instalado", dp.id
        )
        self._refresh_topology()

    # ---------------------------------------------------------
    # Discovery de switches e enlaces (Eventos de Topologia)
    # ---------------------------------------------------------

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self._refresh_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        self._refresh_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        self._refresh_topology()

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        self._refresh_topology()

    def _refresh_topology(self):
        try:
            switch_list = get_switch(self.topology_api_app, None)
            link_list = get_link(self.topology_api_app, None)

            self.switches.update({sw.dp.id: sw.dp for sw in switch_list})

            # Atualiza o Grafo NetworkX
            self.net.clear()
            for sw in switch_list:
                self.net.add_node(sw.dp.id)

            for link in link_list:
                src_dpid = link.src.dpid
                dst_dpid = link.dst.dpid
                src_port = link.src.port_no

                # Aresta direcionada guardando a porta de saída do switch de origem
                self.net.add_edge(src_dpid, dst_dpid, port=src_port)

            self.logger.info(
                "Discovery (NetworkX): %d nós, %d enlaces direcionados",
                self.net.number_of_nodes(),
                self.net.number_of_edges(),
            )

            self.print_full_topology()

        except Exception as exc:
            self.logger.debug("Topologia ainda não disponível: %s", exc)

    def print_full_topology(self):
        self.logger.info("Switches no grafo: %s", sorted(list(self.net.nodes)))
        for u, v, data in sorted(self.net.edges(data=True)):
            self.logger.info("Enlace: s%s -> s%s (porta %s)", u, v, data.get("port"))

    # ---------------------------------------------------------
    # Cálculo de Rotas (NetworkX, OSPF e ECMP Hash)
    # ---------------------------------------------------------

    def get_route_ecmp(self, switch_in, switch_out, paths):
        """Função de hash conforme especificação do enunciado no PDF."""
        if paths:
            hash_val = hash(str(switch_in) + str(switch_out))
            choice = hash_val % len(paths)
            path = sorted(paths)[choice]
            return path
        return None

    def find_route(self, src_dpid, dst_dpid, method):
        """Calcula o caminho (lista de DPIDs) entre a origem e o destino."""
        if not self.net.has_node(src_dpid) or not self.net.has_node(dst_dpid):
            return None

        try:
            if method == "ospf":
                # Retorna o primeiro menor caminho
                path = nx.shortest_path(self.net, source=src_dpid, target=dst_dpid)
                return path

            elif method == "ecmp":
                # Retorna todos os menores caminhos de mesmo custo e aplica o hash exigido no PDF
                all_paths = list(nx.all_shortest_paths(self.net, source=src_dpid, target=dst_dpid))
                return self.get_route_ecmp(str(src_dpid), str(dst_dpid), all_paths)

        except (nx.NetworkXNoPath, nx.NodeNotFound):
            self.logger.warning("Caminho não encontrado entre s%s e s%s", src_dpid, dst_dpid)
            return None

        return None

    # ---------------------------------------------------------
    # Instalação de regras OpenFlow
    # ---------------------------------------------------------

    def _add_flow(self, dp, priority, match, actions, buffer_id=None):
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

    # ---------------------------------------------------------
    # Aprendizado de Hosts
    # ---------------------------------------------------------

    def _switch_link_ports(self):
        """Retorna as portas utilizadas nos enlaces de tráfego interno entre switches."""
        ports = set()
        for u, v, data in self.net.edges(data=True):
            ports.add((u, data.get("port")))
        return ports

    def _learn_host(self, ip, mac, dpid, port):
        if not ip or ip == "0.0.0.0":
            return

        # Evita aprender hosts em portas conectadas a outros switches
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

    # ---------------------------------------------------------
    # Encaminhamento Unicast e Broadcast (Flooding Seguro)
    # ---------------------------------------------------------

    def _route_actions(self, dpid, dst_ip, dst_host):
        dp = self.switches.get(dpid)
        if dp is None:
            return []

        parser = dp.ofproto_parser

        # 1. Se o host de destino está diretamente conectado ao switch atual:
        if dpid == dst_host["dpid"]:
            actions = [parser.OFPActionOutput(dst_host["port"])]
            match = parser.OFPMatch(eth_dst=dst_host["mac"])
            self._add_flow(dp, 20, match, actions)
            return actions

        # 2. Calcula a rota completa via NetworkX
        path = self.find_route(dpid, dst_host["dpid"], self.routing)
        if not path or len(path) < 2:
            return []

        next_hop = path[1]
        out_port = self.net[dpid][next_hop]["port"]

        actions = [parser.OFPActionOutput(out_port)]
        match = parser.OFPMatch(eth_dst=dst_host["mac"])
        self._add_flow(dp, 20, match, actions)

        return actions

    def _flood_actions(self, dpid, in_port):
        """Inunda os pacotes (ARP/Broadcast) através de uma Spanning Tree para evitar loops."""
        dp = self.switches.get(dpid)
        if dp is None or not self.net.nodes:
            return []

        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # Cria árvore geradora (Spanning Tree) não direcionada a partir do grafo
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

            # Verifica se a porta pertence a um enlace entre switches
            is_link_port = False
            is_st_port = False
            for u, v, data in self.net.edges(data=True):
                if u == dpid and data.get("port") == port:
                    is_link_port = True
                    if tuple(sorted((u, v))) in st_edges:
                        is_st_port = True
                    break

            # Se for porta entre switches, só transmite se fizer parte da Spanning Tree
            if is_link_port and not is_st_port:
                continue

            actions.append(parser.OFPActionOutput(port))

        return actions

    # ---------------------------------------------------------
    # Tratamento de Pacotes (PacketIn)
    # ---------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        # Ignora pacotes LLDP (usados pelo próprio Ryu)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_ip = None
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        if arp_pkt:
            src_ip = arp_pkt.src_ip
        elif ip_pkt:
            src_ip = ip_pkt.src

        # Aprende o host de origem
        self._learn_host(src_ip, eth.src, dpid, in_port)

        dst_ip = self.mac_to_host.get(eth.dst)
        dst_host = self.hosts.get(dst_ip) if dst_ip else None

        # Se conhecemos o destino e não é broadcast: instala rota unicast
        if dst_host and eth.dst != "ff:ff:ff:ff:ff:ff":
            actions = self._route_actions(dpid, dst_ip, dst_host)
        else:
            # Caso contrário: realiza o flood seguro
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
        
    # ---------------------------------------------------------
    # Tratamento de Falhas (Item 2 do PDF)
    # ---------------------------------------------------------

# ---------------------------------------------------------
    # Tratamento Dinâmico de Falhas (Item 4 da Lista / Item 2 do PDF)
    # ---------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        port_no = msg.desc.port_no

        # Verifica se a porta foi modificada ou deletada e se o link caiu (OFPPS_LINK_DOWN)
        if msg.reason in (ofp.OFPPR_DELETE, ofp.OFPPR_MODIFY):
            if msg.desc.state & ofp.OFPPS_LINK_DOWN:
                self.logger.warning(
                    "FALHA DETECTADA: Switch s%s - Porta %s caiu (Link Down)!",
                    dp.id, port_no
                )
                
                # 1. Atualiza imediatamente o grafo e a topologia interna
                self._refresh_topology()
                
                # 2. Envia o comando OFPFC_DELETE para limpar as regras de roteamento antigas
                # Remove os fluxos instalados com prioridade 20 (nossos fluxos unicast)
                match = parser.OFPMatch()
                instructions = []
                
                flow_mod = parser.OFPFlowMod(
                    datapath=dp,
                    command=ofp.OFPFC_DELETE,
                    out_port=ofp.OFPP_ANY,
                    out_group=ofp.OFPG_ANY,
                    priority=20,
                    match=match,
                    instructions=instructions
                )
                dp.send_msg(flow_mod)
                
                self.logger.info(
                    "Regras de fluxo antigas removidas via OFPFC_DELETE no switch s%s. A rede recalculará as rotas no próximo pacote.",
                    dp.id
                )
