# Experimentos SDN: Descoberta de Topologia, Roteamento OSPF/ECMP e Tolerância a Falhas

Este repositório contém a implementação e a automação de experimentos em **Redes Definidas por Software (SDN)** utilizando **Mininet**, **Open vSwitch (OpenFlow 1.3)** e o controlador **Ryu**. O objetivo é avaliar o desempenho de encaminhamento (**Flow Completion Time — FCT** e **distribuição de vazão por switch**) comparando os algoritmos **OSPF** e **ECMP** nas topologias **Torus** e **Spine-Leaf**, tanto em operação normal quanto sob falha dinâmica de enlace.

---

## Estrutura do Repositório

```text
SDN-Part2/
├── fctmain.py           # Orquestrador principal (Mininet, Ryu, iperf, bwm-ng e gráficos de vazão)
├── ryu_controller.py    # Controlador Ryu OpenFlow 1.3 (LLDP, OSPF/ECMP, Spanning Tree e failover)
├── plot_fct.py          # Gerador do gráfico de barras comparativo de FCT entre as 4 combinações
├── executar_todos.sh    # Automação da bateria completa (8 experimentos + gráfico comparativo)
├── topologias/          # Definições de enlaces das redes emuladas
│   ├── torus.txt        # Topologia Torus 3x3 (s1 a s9)
│   └── spineleaf.txt    # Topologia Spine-Leaf
├── graficos/            # Gráficos gerados em alta resolução (PNG, 300 DPI)
│   ├── vazao_*.png      # Vazão vs. Tempo por switch para cada cenário
│   └── comparacao_fct_exp1.png
├── logs/                # Logs do controlador (ryu.log) e tempos FCT consolidados (fct_*.txt)
└── registros/           # Coletas brutas de telemetria de banda (bwm_*.csv)
```

---

## Arquitetura da Solução

### 1. Controlador SDN (`ryu_controller.py`)
Desenvolvido sobre o framework **Ryu** utilizando **OpenFlow 1.3** e **NetworkX**:
* **Descoberta Dinâmica (LLDP):** Constrói e atualiza um grafo direcionado (`nx.DiGraph`) mapeando switches, enlaces e portas de saída a partir dos eventos `EventSwitchEnter`, `EventLinkAdd` e `EventLinkDelete`.
* **Roteamento OSPF (`SDN_ROUTING=ospf`):** Calcula o menor caminho único entre o switch de origem e o de destino usando `nx.shortest_path`.
* **Roteamento ECMP (`SDN_ROUTING=ecmp`):** Identifica todos os caminhos mínimos de mesmo custo (`nx.all_shortest_paths`) e seleciona a rota de forma determinística por par de switches através de função de hash (`get_route_ecmp`), distribuindo a carga entre múltiplos caminhos.
* **Flooding Seguro sem Loops:** Em topologias cíclicas (como a Torus), pacotes ARP e broadcast são inundados apenas pelas arestas de uma **Árvore Geradora Mínima** (`nx.minimum_spanning_tree`), evitando tempestades de broadcast.
* **Reconvergência Dinâmica a Falhas:** Ao detectar queda de porta (`EventOFPPortStatus` com `OFPPS_LINK_DOWN`) ou remoção de enlace (`EventLinkDelete`), o controlador remove imediatamente ambas as direções da aresta do grafo, limpa as regras obsoletas de todos os switches (`OFPFC_DELETE`) e reinstala a regra `table-miss` (`priority=0`) para redirecionar os fluxos ativos sem perda permanente de conectividade.

### 2. Orquestrador de Experimentos (`fctmain.py`)
* Lê a topologia a partir de `topologias/`, cria um host `hN` (`10.0.0.N/8`) conectado a cada switch `sN` e aplica limitação de banda via `TCLink` (padrão: `200 Mbps`) para evitar saturação de CPU da VM e estouro de contadores.
* Inicia uma instância limpa do Ryu com `--observe-links` e desabilita IPv6 nos nós para eliminar tráfego de controle indesejado.
* Valida a conectividade inicial com `pingAll()`, inicia a coleta de telemetria (`bwm-ng` a cada `1s`) e dispara tráfego TCP **todos-para-todos** (`N × (N - 1)` fluxos `iperf` simultâneos) em rodada única.
* Registra o **FCT total** em `logs/fct_<topologia>_<roteamento>_exp<N>.txt` e plota automaticamente o gráfico de vazão por switch em `graficos/vazao_<topologia>_<roteamento>_exp<N>.png`.

---

## Cenários Experimentais

São executadas **8 combinações** no total (`2 topologias × 2 algoritmos × 2 experimentos`):

| Experimento | Descrição | Comportamento |
| :--- | :--- | :--- |
| **Exp 1 (Baseline)** | Operação normal sem falhas | Transfere o volume configurado (`--size 60M` por par) entre todos os pares de hosts simultaneamente, medindo o FCT total e o balanceamento de carga entre os switches. |
| **Exp 2 (Falha de Enlace)** | Tolerância a falhas e reconvergência | Aguarda `--fail-delay` segundos (padrão: `8s`) de tráfego ativo, derruba um enlace crítico (`s4 <-> s5` na Torus; `s1 <-> s3` na Spine-Leaf) via `configLinkStatus(..., "down")` e acompanha o desvio de rota até a conclusão de todos os fluxos. |

---

## Pré-requisitos

* **Sistema:** Linux / VM Mininet com Python 3 e Open vSwitch.
* **Ferramentas de rede:** `mininet`, `iperf`, `bwm-ng`.
* **Pacotes Python:** `ryu` (com `eventlet==0.30.2` para compatibilidade), `networkx`, `pandas`, `matplotlib`, `numpy`.

---

## Como Executar

### 1. Executar toda a bateria de 8 experimentos + gráfico comparativo
O script `executar_todos.sh` limpa o ambiente (`mn -c`) entre cada rodada, executa as 8 combinações e gera o gráfico de barras de FCT ao final:

```bash
chmod +x executar_todos.sh
sudo ./executar_todos.sh
```

*(Opcionalmente, é possível repassar parâmetros extras ao `fctmain.py`, como `sudo ./executar_todos.sh --bw 200 --size 60M --fail-delay 8`.)*

### 2. Executar um experimento individual
```bash
sudo python3 fctmain.py --topology torus --routing ecmp --exp 1
sudo python3 fctmain.py --topology torus --routing ecmp --exp 2
sudo python3 fctmain.py --topology spineleaf --routing ospf --exp 1
sudo python3 fctmain.py --topology spineleaf --routing ospf --exp 2
```

### 3. Gerar o gráfico comparativo de barras (FCT)
Após concluir as 4 combinações do Experimento 1 (ou 2), execute:

```bash
python3 plot_fct.py --exp 1
```

---

## Visualização dos Resultados

* **Comparação de FCT (Experimento 1):** Contrasta o tempo final de conclusão de fluxo (eixo Y) entre **Torus (ECMP)**, **Torus (OSPF)**, **Spine-Leaf (ECMP)** e **Spine-Leaf (OSPF)** (eixo X):
  `graficos/comparacao_fct_exp1.png`
* **Distribuição de Vazão por Switch (Exp 1 e Exp 2):** Mostra a carga agregada (Mbps) em cada switch segundo a segundo, destacando com uma linha vertical tracejada o instante da queda do enlace no Experimento 2:
  `graficos/vazao_<topologia>_<roteamento>_exp<1|2>.png`
