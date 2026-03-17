const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
        ShadingType, PageNumber, PageBreak, LevelFormat } = require('docx');
const fs = require('fs');

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 60, bottom: 60, left: 100, right: 100 };

function headerCell(text, width) {
    return new TableCell({
        borders, width: { size: width, type: WidthType.DXA },
        shading: { fill: "1F4E79", type: ShadingType.CLEAR },
        margins: cellMargins,
        children: [new Paragraph({ children: [new TextRun({ text, bold: true, color: "FFFFFF", font: "Arial", size: 18 })] })]
    });
}

function dataCell(text, width, fill) {
    return new TableCell({
        borders, width: { size: width, type: WidthType.DXA },
        shading: fill ? { fill, type: ShadingType.CLEAR } : undefined,
        margins: cellMargins,
        children: [new Paragraph({ children: [new TextRun({ text: String(text), font: "Arial", size: 18 })] })]
    });
}

function boldCell(text, width, fill) {
    return new TableCell({
        borders, width: { size: width, type: WidthType.DXA },
        shading: fill ? { fill, type: ShadingType.CLEAR } : undefined,
        margins: cellMargins,
        children: [new Paragraph({ children: [new TextRun({ text: String(text), bold: true, font: "Arial", size: 18 })] })]
    });
}

const doc = new Document({
    styles: {
        default: { document: { run: { font: "Arial", size: 22 } } },
        paragraphStyles: [
            { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
              run: { size: 32, bold: true, font: "Arial", color: "1F4E79" },
              paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
            { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
              run: { size: 26, bold: true, font: "Arial", color: "2E75B6" },
              paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
        ]
    },
    numbering: {
        config: [
            { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
              style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
        ]
    },
    sections: [{
        properties: {
            page: {
                size: { width: 12240, height: 15840 },
                margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
            }
        },
        headers: {
            default: new Header({ children: [new Paragraph({
                border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "1F4E79", space: 1 } },
                children: [new TextRun({ text: "Composite Leader Variants \u2014 Live-Clean Validation Memo", font: "Arial", size: 16, color: "999999" })]
            })] })
        },
        footers: {
            default: new Footer({ children: [new Paragraph({
                alignment: AlignmentType.CENTER,
                children: [new TextRun({ text: "Page ", font: "Arial", size: 16, color: "999999" }),
                           new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: "999999" })]
            })] })
        },
        children: [
            // Title
            new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
                children: [new TextRun({ text: "VALIDATION MEMO", size: 40, bold: true, font: "Arial", color: "1F4E79" })] }),
            new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
                children: [new TextRun({ text: "Composite Leader Variants \u2014 Live-Clean", size: 28, font: "Arial", color: "2E75B6" })] }),
            new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 400 },
                children: [new TextRun({ text: "March 11, 2026 \u2014 No Hindsight GREEN-Day Logic", size: 20, font: "Arial", color: "666666" })] }),

            // Verdict box
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [9360],
                rows: [new TableRow({ children: [new TableCell({
                    borders: { top: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               bottom: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               left: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               right: { style: BorderStyle.SINGLE, size: 3, color: "C62828" } },
                    shading: { fill: "FFEBEE", type: ShadingType.CLEAR },
                    width: { size: 9360, type: WidthType.DXA },
                    margins: { top: 120, bottom: 120, left: 200, right: 200 },
                    children: [
                        new Paragraph({ alignment: AlignmentType.CENTER,
                            children: [new TextRun({ text: "VERDICT: NOT PROMOTABLE", size: 32, bold: true, font: "Arial", color: "C62828" })] }),
                        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 60 },
                            children: [new TextRun({ text: "All 14 live-clean variants fail. The composite edge was entirely driven by hindsight GREEN-day selection.", size: 20, font: "Arial", color: "C62828" })] }),
                    ]
                })] })]
            }),

            new Paragraph({ spacing: { before: 300 } }),

            // Background
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("1. Background")] }),
            new Paragraph({ spacing: { after: 120 }, children: [
                new TextRun("The Composite Long Study tested multi-layer filter combinations stacking market support (M), in-play selection (P), leadership (L), and acceptance entry (E). The original study used M1 = GREEN day (end-of-day SPY return > 0.05%) as the market layer, which is "),
                new TextRun({ text: "hindsight \u2014 it uses perfect foresight of how the day ends", bold: true }),
                new TextRun(". This is not available in live trading."),
            ] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun("This validation rebuilds the best variants with live-available market support only: ML2 (SPY above VWAP at entry time) and ML3 (SPY above VWAP + EMA9 > EMA20 at entry time). Leadership (RS vs SPY) and in-play (gap + RVOL) filters remain unchanged."),
            ] }),

            // Original vs live-clean
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("2. Original Results (M1 Hindsight) vs Live-Clean")] }),
            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Original (hindsight GREEN-day)")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [3500, 1200, 1200, 1730, 1730],
                rows: [
                    new TableRow({ children: [headerCell("Variant", 3500), headerCell("N", 1200), headerCell("PF", 1200), headerCell("Exp(R)", 1730), headerCell("Status", 1730)] }),
                    new TableRow({ children: [dataCell("VK_green_leader", 3500), dataCell("2229", 1200), boldCell("1.32", 1200, "E8F5E9"), dataCell("+0.162", 1730), dataCell("PROMOTE", 1730, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("EMA9_green_inplay_leader", 3500), dataCell("63", 1200), boldCell("1.57", 1200, "E8F5E9"), dataCell("+0.256", 1730), dataCell("PROMOTE*", 1730, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("VK_green", 3500), dataCell("3688", 1200), boldCell("1.22", 1200, "E8F5E9"), dataCell("+0.113", 1730), dataCell("PROMOTE", 1730, "E8F5E9")] }),
                ]
            }),

            new Paragraph({ spacing: { before: 200 } }),
            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Live-clean (ML2 = SPY above VWAP)")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [3500, 1200, 1200, 1730, 1730],
                rows: [
                    new TableRow({ children: [headerCell("Variant", 3500), headerCell("N", 1200), headerCell("PF", 1200), headerCell("Exp(R)", 1730), headerCell("Status", 1730)] }),
                    new TableRow({ children: [dataCell("VK_ML2_leader", 3500), dataCell("7464", 1200), boldCell("0.78", 1200, "FFEBEE"), dataCell("-0.137", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                    new TableRow({ children: [dataCell("EMA9_ML2_inplay_leader", 3500), dataCell("284", 1200), boldCell("0.83", 1200, "FFEBEE"), dataCell("-0.103", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                    new TableRow({ children: [dataCell("VK_ML2", 3500), dataCell("10060", 1200), boldCell("0.75", 1200, "FFEBEE"), dataCell("-0.179", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                    new TableRow({ children: [dataCell("EMA9_ML2_leader", 3500), dataCell("7600", 1200), boldCell("0.77", 1200, "FFEBEE"), dataCell("-0.150", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                    new TableRow({ children: [dataCell("VK_ML3_leader", 3500), dataCell("5111", 1200), boldCell("0.74", 1200, "FFEBEE"), dataCell("-0.157", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                    new TableRow({ children: [dataCell("EMA9_ML3_inplay_leader", 3500), dataCell("156", 1200), boldCell("0.82", 1200, "FFEBEE"), dataCell("-0.109", 1730), dataCell("RETIRE", 1730, "FFEBEE")] }),
                ]
            }),

            new Paragraph({ children: [new PageBreak()] }),

            // Full live-clean matrix
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("3. Complete Live-Clean Variant Matrix")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [3000, 900, 900, 1100, 1100, 1100, 1260],
                rows: [
                    new TableRow({ children: [headerCell("Variant", 3000), headerCell("N", 900), headerCell("PF", 900), headerCell("Exp(R)", 1100), headerCell("TrnPF", 1100), headerCell("TstPF", 1100), headerCell("Status", 1260)] }),
                    ...[
                        ["VK_ML2", "10060", "0.75", "-0.179", "0.69", "0.81", "RETIRE"],
                        ["VK_ML2_leader", "7464", "0.78", "-0.137", "0.73", "0.83", "RETIRE"],
                        ["VK_ML2_inp_ldr", "296", "0.66", "-0.207", "0.57", "0.77", "RETIRE"],
                        ["VK_ML3", "7123", "0.71", "-0.199", "0.66", "0.76", "RETIRE"],
                        ["VK_ML3_leader", "5111", "0.74", "-0.157", "0.70", "0.79", "RETIRE"],
                        ["VK_ML3_inp_ldr", "179", "0.50", "-0.306", "0.36", "0.70", "RETIRE"],
                        ["EMA9_ML2", "11495", "0.76", "-0.173", "0.75", "0.77", "RETIRE"],
                        ["EMA9_ML2_leader", "7600", "0.77", "-0.150", "0.74", "0.80", "RETIRE"],
                        ["EMA9_ML2_inp_ldr", "284", "0.83", "-0.103", "0.76", "0.91", "RETIRE"],
                        ["EMA9_ML3", "7563", "0.72", "-0.196", "0.73", "0.71", "RETIRE"],
                        ["EMA9_ML3_leader", "4886", "0.75", "-0.162", "0.76", "0.73", "RETIRE"],
                        ["EMA9_ML3_inp_ldr", "156", "0.82", "-0.109", "0.69", "0.95", "RETIRE"],
                        ["VK_leader_noM", "8303", "0.75", "-0.166", "0.70", "0.81", "RETIRE"],
                        ["EMA9_leader_noM", "8664", "0.77", "-0.160", "0.74", "0.80", "RETIRE"],
                    ].map(([name, n, pf, exp, trn, tst, status]) => {
                        const fill = "FFEBEE";
                        return new TableRow({ children: [
                            dataCell(name, 3000), dataCell(n, 900), boldCell(pf, 900, fill),
                            dataCell(exp, 1100), dataCell(trn, 1100), dataCell(tst, 1100),
                            boldCell(status, 1260, fill)
                        ] });
                    })
                ]
            }),

            new Paragraph({ spacing: { before: 200 } }),

            // Analysis
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("4. Analysis")] }),
            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Why the edge disappears")] }),
            new Paragraph({ spacing: { after: 120 }, children: [
                new TextRun("The original composite study filtered to GREEN days only (SPY ends higher). This is a "),
                new TextRun({ text: "selection bias that ensures any long pattern looks good", bold: true }),
                new TextRun(". On days the market finishes green, acceptance entries (crossing above VWAP or 9EMA) naturally succeed because the entire market is lifting. The leadership filter compounds this \u2014 stocks outperforming SPY on a green day are in the strongest possible configuration."),
            ] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun("When this hindsight filter is replaced with real-time SPY conditions (above VWAP at the moment of entry), the filter admits many days that start strong but reverse. SPY being above VWAP at 10:30 AM does not predict whether the day finishes green. The acceptance entries on these reversal days become losses, destroying the aggregate edge."),
            ] }),

            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Leadership helps but not enough")] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun("The leadership filter (RS vs SPY > 0) consistently adds 0.02\u20130.05 to PF across all variants. This confirms your thesis that real traders use supporting conditions. However, the leadership lift is not enough to overcome the base entry\u2019s negative expectancy without the GREEN filter. The underlying VK/EMA9 acceptance patterns need more structural edge before layered filters can push them above water."),
            ] }),

            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Why FLR succeeds where composites fail")] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun("FL_MOMENTUM_REBUILD has a key structural advantage: it requires a "),
                new TextRun({ text: "meaningful decline (3.0 ATR) before entry", bold: true }),
                new TextRun(". This ensures the setup only fires when there\u2019s been genuine selling pressure \u2014 a natural event filter that eliminates the need for a market direction gate. The 4-bar turn confirmation further ensures the selling has actually stopped. The composite entries (VK cross, EMA9 cross) fire on any reclaim, including minor dips on trending days that are just noise."),
            ] }),

            // Verdict
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("5. Final Verdict")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [9360],
                rows: [new TableRow({ children: [new TableCell({
                    borders: { top: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               bottom: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               left: { style: BorderStyle.SINGLE, size: 3, color: "C62828" },
                               right: { style: BorderStyle.SINGLE, size: 3, color: "C62828" } },
                    shading: { fill: "FFEBEE", type: ShadingType.CLEAR },
                    width: { size: 9360, type: WidthType.DXA },
                    margins: { top: 120, bottom: 120, left: 200, right: 200 },
                    children: [
                        new Paragraph({ children: [
                            new TextRun({ text: "DEAD. ", bold: true, size: 22, font: "Arial", color: "C62828" }),
                            new TextRun({ text: "All composite leader variants are not promotable under live-clean conditions. The entire edge was an artifact of GREEN-day hindsight. Leadership is a confirmed positive signal but needs to be layered on a pattern with structural edge (like FL_MOMENTUM_REBUILD) rather than on generic acceptance entries.", size: 22, font: "Arial" }),
                        ] }),
                    ]
                })] })]
            }),

            new Paragraph({ spacing: { before: 200 } }),
            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Recommended path forward")] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "Apply leadership filter to FL_MOMENTUM_REBUILD: ", bold: true }),
                new TextRun("FLR has the structural edge; adding RS > 0 as a confirmation layer is the natural next experiment."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "Develop better market direction proxies: ", bold: true }),
                new TextRun("SPY above VWAP is too weak. Consider SPY above VWAP with rising slope, or SPY making new intraday highs, or multi-timeframe trend alignment."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "Abandon generic acceptance entries for standalone use: ", bold: true }),
                new TextRun("VK and EMA9 cross patterns lack structural edge without event-level selectivity (like FLR\u2019s decline threshold)."),
            ] }),
        ]
    }]
});

Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync("/sessions/inspiring-clever-meitner/mnt/alert_overlay/outputs/Composite_Leader_Validation_Memo.docx", buffer);
    console.log("Composite memo written.");
});
