import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# 1. Carregar o arquivo CSV (sem cabeçalho)
df = pd.read_csv('bwm_spineleaf_ecmp_exp2.csv', header=None)

# 2. Filtrar apenas interfaces de switches (que começam com 's')
df_switches = df[df[1].str.startswith('s', na=False)].copy()

# 3. Extrair o nome do switch (ex: 's1-eth1' -> 's1')
df_switches['switch'] = df_switches[1].apply(lambda x: x.split('-')[0])

# 4. Normalizar o tempo para começar em t = 0
t0 = df_switches[0].min()
df_switches['tempo_relativo'] = df_switches[0] - t0

# 5. Converter a coluna de bytes para Mbps (coluna 2 do bwm-ng)
df_switches['vazao_mbps'] = (pd.to_numeric(df_switches[2], errors='coerce') * 8) / 1e6 

# 6. Agrupar o tempo em intervalos (bins de 0.1 segundos) para alinhar as amostras de todos os switches
df_switches['tempo_bin'] = np.round(df_switches['tempo_relativo'] / 0.1) * 0.1

# 7. Agrupar por tempo binned e switch, somando as interfaces e preenchendo vazios com 0
df_grouped = df_switches.groupby(['tempo_bin', 'switch'])['vazao_mbps'].sum().unstack().fillna(0)

# 8. Plotar o gráfico "Vazão vs. Tempo" com todos os switches no mesmo gráfico
plt.figure(figsize=(12, 6))
for switch_name in df_grouped.columns:
    plt.plot(df_grouped.index, df_grouped[switch_name], label=switch_name, linewidth=2)

plt.title('Vazão vs. Tempo - Distribuição de Carga por Switch (ECMP E2)')
plt.xlabel('Tempo (segundos)')
plt.ylabel('Vazão Total (Mbps)')
plt.grid(True, linestyle='--', alpha=0.7)
plt.legend(title='Switches', bbox_to_anchor=(1.05, 1), loc='upper left')
plt.tight_layout()

# 9. Salvar como imagem PNG
output_filename = 'vazao_vs_tempo_exp2.png'
plt.savefig(output_filename, dpi=300)
print(f"Gráfico gerado com sucesso e salvo como '{output_filename}'!")
