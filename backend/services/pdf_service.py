import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

def generate_farm_pdf_report(farm_data: dict, weather_data: dict, analytics_data: dict, sim_data: dict, yield_data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontSize=16, leading=20, textColor=colors.HexColor('#1f6feb'))
    subtitle_style = ParagraphStyle('DocSubtitle', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#57606a'))
    section_title = ParagraphStyle('SectionTitle', parent=styles['Heading2'], fontSize=11, leading=15, textColor=colors.HexColor('#24292f'), spaceBefore=8, spaceAfter=4)
    body_style = ParagraphStyle('BodyDark', parent=styles['Normal'], fontSize=8.5, leading=11, textColor=colors.HexColor('#24292f'))

    elements = []

    # Cabeçalho
    elements.append(Paragraph(f"<b>LAUDO TÉCNICO & ESTIMATIVA AGRONÔMICA</b> — {farm_data.get('name', 'Fazenda')}", title_style))
    elements.append(Paragraph(f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')} | Plataforma Orion Agro — Sensoriamento Espectral & NASA POWER", subtitle_style))
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#1f6feb'), spaceAfter=10))

    # Dados Gerais
    data_geral = [
        [Paragraph("<b>Propriedade:</b>", body_style), Paragraph(farm_data.get('name', '--'), body_style),
         Paragraph("<b>Localização:</b>", body_style), Paragraph(farm_data.get('city', '--'), body_style)],
        [Paragraph("<b>Área Total:</b>", body_style), Paragraph(f"{farm_data.get('total_area', '--')} ha", body_style),
         Paragraph("<b>Sistema:</b>", body_style), Paragraph("Cultivo Duplo Anual (Soja / Milho)", body_style)],
        [Paragraph("<b>Coordenadas:</b>", body_style), Paragraph(f"{farm_data.get('latitude', '--')}, {farm_data.get('longitude', '--')}", body_style),
         Paragraph("<b>Faturamento Anual Est.:</b>", body_style), Paragraph(f"R$ {yield_data.get('total_anual_faturamento_brl', 0):,.2f}", body_style)]
    ]
    t_geral = Table(data_geral, colWidths=[100, 170, 100, 170])
    t_geral.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f6f8fa')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_geral)
    elements.append(Spacer(1, 10))

    # 1. Estimativa de Produtividade Real
    elements.append(Paragraph("<b>1. Estimativa de Produtividade por Safra (Sensoriamento Remoto)</b>", section_title))
    verao = yield_data.get('safra_verao', {})
    safrinha = yield_data.get('safrinha', {})

    data_prod = [
        ["Safra / Ciclo", "Cultura Identificada", "Pico NDVI", "Produtividade", "Volume Total", "Faturamento Bruto"],
        ["Safra de Verão", f"{verao.get('cultura')} ({verao.get('janela')})", f"{verao.get('pico_ndvi', 0):.2f}", f"{verao.get('produtividade_sc_ha', 0)} sc/ha", f"{verao.get('total_sacas', 0):,} sc", f"R$ {verao.get('faturamento_bruto_brl', 0):,.2f}"],
        ["Segunda Safra (Safrinha)", f"{safrinha.get('cultura')} ({safrinha.get('janela')})", f"{safrinha.get('pico_ndvi', 0):.2f}", f"{safrinha.get('produtividade_sc_ha', 0)} sc/ha", f"{safrinha.get('total_sacas', 0):,} sc", f"R$ {safrinha.get('faturamento_bruto_brl', 0):,.2f}"],
    ]
    t_prod = Table(data_prod, colWidths=[90, 140, 60, 80, 75, 95])
    t_prod.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f6feb')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (2, 0), (-1, -1), 'CENTER'),
    ]))
    elements.append(t_prod)
    elements.append(Spacer(1, 10))

    # 2. Zoneamento Espectral
    elements.append(Paragraph("<b>2. Zoneamento Espectral do Talhão (Passagem Selecionada)</b>", section_title))
    zones = analytics_data.get('current_zones', {})
    data_zones = [
        ["Zona Espectral", "Diagnóstico de Vigor", "Proporção (%)", "Área (ha)"],
        ["🔴 Zona 1", "Estresse Severo / Solo Exposto (< 0.35)", f"{zones.get('stress', {}).get('pct', 0)}%", f"{zones.get('stress', {}).get('ha', 0)} ha"],
        ["🟡 Zona 2", "Vigor Moderado / Atenção (0.35 - 0.55)", f"{zones.get('medium', {}).get('pct', 0)}%", f"{zones.get('medium', {}).get('ha', 0)} ha"],
        ["🟢 Zona 3", "Bom Vigor / Lavouras Sadias (0.55 - 0.75)", f"{zones.get('good', {}).get('pct', 0)}%", f"{zones.get('good', {}).get('ha', 0)} ha"],
        ["🔵 Zona 4", "Biomassa Densa / Alta Sanidade (> 0.75)", f"{zones.get('dense', {}).get('pct', 0)}%", f"{zones.get('dense', {}).get('ha', 0)} ha"],
    ]
    t_zones = Table(data_zones, colWidths=[70, 260, 100, 110])
    t_zones.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0969da')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (2, 0), (-1, -1), 'CENTER'),
    ]))
    elements.append(t_zones)
    elements.append(Spacer(1, 10))

    # 3. Clima NASA POWER
    elements.append(Paragraph("<b>3. Resumo Agrometeorológico (NASA POWER)</b>", section_title))
    summary = weather_data.get('summary', {})
    data_clim = [
        [Paragraph(f"<b>Chuva Acumulada:</b> {summary.get('total_rain_mm', '--')} mm", body_style),
         Paragraph(f"<b>Temp. Média:</b> {summary.get('avg_temp_c', '--')} °C", body_style),
         Paragraph(f"<b>Extremos Térmicos:</b> Mín {summary.get('min_temp_c', '--')}°C / Máx {summary.get('max_temp_c', '--')}°C", body_style)]
    ]
    t_clim = Table(data_clim, colWidths=[180, 180, 180])
    t_clim.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f6f8fa')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(t_clim)
    elements.append(Spacer(1, 10))

    # 4. Projeção What-If
    elements.append(Paragraph("<b>4. Simulação de Cenários Manejo (What-If)</b>", section_title))
    data_sim = [
        ["Parâmetro de Manejo", "Ajuste Simulado", "Impacto Projetado"],
        ["Adubação Nitrogenada (N)", f"{sim_data.get('n_kg', 0):+d} kg/ha", f"Variação NDVI: {sim_data.get('delta_ndvi', 0):+.2f}"],
        ["Lâmina de Irrigação", f"{sim_data.get('w_mm', 0):+d} mm", f"Produtividade: {sim_data.get('delta_yield_sc_ha', 0):+.1f} sc/ha"],
        ["Pressão de Pragas", f"{sim_data.get('pest_pct', 0)}%", f"Ganho Líquido: R$ {sim_data.get('net_financial_gain_brl', 0):,.2f}"]
    ]
    t_sim = Table(data_sim, colWidths=[180, 160, 200])
    t_sim.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2da44e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d7de')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_sim)
    elements.append(Spacer(1, 12))

    elements.append(Paragraph("<i>Documento técnico com modelos biofísicos de sensoriamento remoto espacial e inteligência agronômica.</i>", subtitle_style))

    doc.build(elements)
    return buffer.getvalue()