#!/usr/bin/env bash
set -euo pipefail

# Executa automaticamente todas as 8 combinações de experimentos SDN:
#   2 topologias (torus, spineleaf) x 2 roteamentos (ospf, ecmp) x 2 experimentos (1, 2)
# Ao final, gera o gráfico comparativo de barras (FCT) com plot_fct.py.
#
# Uso: sudo ./executar_todos.sh [args_extras_para_fctmain]
# Exemplo: sudo ./executar_todos.sh --bw 200 --size 60M

cd "$(dirname "$0")"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "ERRO: Este script precisa ser executado com sudo (ex: sudo $0)."
  exit 1
fi

TOPOLOGIES=("torus" "spineleaf")
ROUTINGS=("ospf" "ecmp")
EXPERIMENTS=(1 2)

TOTAL=$(( ${#TOPOLOGIES[@]} * ${#ROUTINGS[@]} * ${#EXPERIMENTS[@]} ))
ATUAL=0

echo "=================================================================="
echo " Iniciando bateria completa de $TOTAL experimentos SDN"
echo "=================================================================="

for topo in "${TOPOLOGIES[@]}"; do
  for routing in "${ROUTINGS[@]}"; do
    for exp in "${EXPERIMENTS[@]}"; do
      ATUAL=$((ATUAL + 1))
      echo ""
      echo "------------------------------------------------------------------"
      echo " [$ATUAL/$TOTAL] Topologia=$topo | Roteamento=$routing | Exp=$exp"
      echo "------------------------------------------------------------------"

      # Garante ambiente limpo antes de cada rodada
      mn -c >/dev/null 2>&1 || true
      pkill -9 -f "[i]perf" >/dev/null 2>&1 || true

      python3 fctmain.py --topology "$topo" --routing "$routing" --exp "$exp" "$@"
    done
  done
done

echo ""
echo "=================================================================="
echo " Gerando gráfico comparativo de barras de FCT (plot_fct.py)..."
echo "=================================================================="
python3 plot_fct.py --exp 1

echo ""
echo "Bateria de $TOTAL experimentos concluída com sucesso!"
