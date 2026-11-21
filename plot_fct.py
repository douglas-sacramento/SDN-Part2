a#!/usr/bin/env python3
"""
Gera o gráfico de barras comparativo de Flow Completion Time (FCT) para o Experimento 1
lendo automaticamente os arquivos `logs/fct_<topologia>_<roteamento>_exp1.txt` e salvando
o resultado em `graficos/comparacao_fct_exp1.png`.
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
GRAFICOS_DIR = BASE_DIR / "graficos"

COMBINACOES = [
    ("torus", "ecmp", "Torus\n(ECMP)"),
    ("torus", "ospf", "Torus\n(OSPF)"),
    ("spineleaf", "ecmp", "Spine-Leaf\n(ECMP)"),
    ("spineleaf", "ospf", "Spine-Leaf\n(OSPF)"),
]


def ler_fct(arquivo_txt: Path) -> float:
    if not arquivo_txt.is_file():
        raise FileNotFoundError(f"Arquivo de resultado não encontrado: {arquivo_txt}")

    for linha in arquivo_txt.read_text(encoding="utf-8").splitlines():
        if linha.startswith("FCT_Seconds:"):
            valor_str = linha.split(":", 1)[1].strip()
            return float(valor_str)

    raise ValueError(f"Chave 'FCT_Seconds:' não encontrada em {arquivo_txt}")


def gerar_grafico_fct(logs_dir: Path, exp: int, output_path: Path, permitir_parcial: bool = False):
    labels = []
    valores = []
    cores = []

    paleta = {
        "ospf": "#2b5c8f",   # Azul escuro para OSPF
        "ecmp": "#2e8b57",   # Verde mar para ECMP
    }

    for topo, routing, label in COMBINACOES:
        arquivo = logs_dir / f"fct_{topo}_{routing}_exp{exp}.txt"
        try:
            fct_val = ler_fct(arquivo)
            labels.append(label)
            valores.append(fct_val)
            cores.append(paleta[routing])
        except FileNotFoundError as exc:
            if permitir_parcial:
                print(f"AVISO: {exc} (ignorado por --parcial)")
                continue
            raise

    if not valores:
        raise RuntimeError("Nenhum arquivo de FCT válido encontrado para gerar o gráfico.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    barras = ax.bar(labels, valores, color=cores, width=0.52, edgecolor="#1f1f1f", linewidth=0.9)

    max_val = max(valores)
    for barra, val in zip(barras, valores):
        ax.text(
            barra.get_x() + barra.get_width() / 2.0,
            val + (max_val * 0.015),
            f"{val:.2f} s",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
            color="#222222",
        )

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=paleta["ecmp"], edgecolor="#1f1f1f"),
        plt.Rectangle((0, 0), 1, 1, color=paleta["ospf"], edgecolor="#1f1f1f"),
    ]
    ax.legend(handles, ["ECMP (Múltiplos Caminhos)", "OSPF (Menor Caminho)"], title="Roteamento", loc="upper right")

    ax.set_title(
        f"Comparação do Tempo Final de Conclusão de Fluxo (FCT) - Experimento {exp}",
        fontsize=13,
        fontweight="bold",
        pad=14,
    )
    ax.set_xlabel("Combinação Topologia / Roteamento", fontsize=11, labelpad=10)
    ax.set_ylabel("Tempo Total de Conclusão de Fluxo - FCT (segundos)", fontsize=11)
    ax.set_ylim(0, max_val * 1.15)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.set_axisbelow(True)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300)
    plt.close(fig)
    print(f"Gráfico comparativo de FCT salvo com sucesso em: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Gera gráfico de barras comparando o FCT das 4 combinações de topologia/roteamento."
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=LOGS_DIR,
        help="Diretório onde estão os arquivos fct_*.txt (padrão: logs/)"
    )
    parser.add_argument(
        "--exp", type=int, default=1, choices=[1, 2],
        help="Número do experimento a sumarizar (padrão: 1)"
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Caminho do arquivo PNG de saída (padrão: graficos/comparacao_fct_exp<exp>.png)"
    )
    parser.add_argument(
        "--parcial", action="store_true",
        help="Permite gerar o gráfico mesmo se alguma das 4 combinações ainda não tiver sido executada"
    )
    args = parser.parse_args()

    output_png = args.out if args.out else (GRAFICOS_DIR / f"comparacao_fct_exp{args.exp}.png")
    try:
        gerar_grafico_fct(args.logs_dir, args.exp, output_png, permitir_parcial=args.parcial)
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
