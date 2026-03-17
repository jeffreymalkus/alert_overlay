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
            { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
              run: { size: 22, bold: true, font: "Arial", color: "2E75B6" },
              paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 } },
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
                children: [new TextRun({ text: "FL_MOMENTUM_REBUILD \u2014 Validation Memo", font: "Arial", size: 16, color: "999999" })]
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
                children: [new TextRun({ text: "FL_MOMENTUM_REBUILD (SetupId 25)", size: 28, font: "Arial", color: "2E75B6" })] }),
            new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 400 },
                children: [new TextRun({ text: "March 11, 2026 \u2014 Engine-Native Standard", size: 20, font: "Arial", color: "666666" })] }),

            // Verdict box
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [9360],
                rows: [new TableRow({ children: [new TableCell({
                    borders: { top: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               bottom: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               left: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               right: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" } },
                    shading: { fill: "E8F5E9", type: ShadingType.CLEAR },
                    width: { size: 9360, type: WidthType.DXA },
                    margins: { top: 120, bottom: 120, left: 200, right: 200 },
                    children: [
                        new Paragraph({ alignment: AlignmentType.CENTER,
                            children: [new TextRun({ text: "VERDICT: SURVIVES", size: 32, bold: true, font: "Arial", color: "2E8B57" })] }),
                        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 60 },
                            children: [new TextRun({ text: "All 5 promotion criteria pass under engine-native standard", size: 20, font: "Arial", color: "2E8B57" })] }),
                    ]
                })] })]
            }),

            new Paragraph({ spacing: { before: 300 } }),

            // Section 1: Setup Description
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("1. Setup Description")] }),
            new Paragraph({ spacing: { after: 120 }, children: [
                new TextRun("FL_MOMENTUM_REBUILD detects long entries after a meaningful morning decline followed by a confirmed turn and 9EMA cross above VWAP. The setup targets momentum rebuilds where selling pressure has exhausted and the stock begins recovering through a key technical level.")
            ] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun({ text: "Promoted config: ", bold: true }),
                new TextRun("4-bar turn confirmation, stop = 50% of measured move, time window 10:30\u201311:30, decline \u2265 3.0 ATR, 60% body on cross bar, no market alignment required.")
            ] }),

            // Section 2: Standalone Results
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("2. Standalone Results")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [3200, 3080, 3080],
                rows: [
                    new TableRow({ children: [headerCell("Metric", 3200), headerCell("Value", 3080), headerCell("Criteria", 3080)] }),
                    new TableRow({ children: [dataCell("Trade Count (N)", 3200), boldCell("621", 3080), dataCell("\u2265 10", 3080)] }),
                    new TableRow({ children: [dataCell("Profit Factor", 3200), boldCell("1.10", 3080, "E8F5E9"), dataCell("> 1.0  \u2713 PASS", 3080, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("Expectancy (R)", 3200), boldCell("+0.054", 3080, "E8F5E9"), dataCell("> 0  \u2713 PASS", 3080, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("Train PF", 3200), boldCell("1.01", 3080, "E8F5E9"), dataCell("> 0.80  \u2713 PASS", 3080, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("Test PF", 3200), boldCell("1.19", 3080, "E8F5E9"), dataCell("> 0.80  \u2713 PASS", 3080, "E8F5E9")] }),
                    new TableRow({ children: [dataCell("Win Rate", 3200), dataCell("47.3%", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Total R", 3200), dataCell("+33.3", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Max Drawdown (R)", 3200), dataCell("33.3", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Avg Hold Bars", 3200), dataCell("34.4 (2h 52m)", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Stop Rate", 3200), dataCell("41.1%", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Target Rate", 3200), dataCell("20.6% (100% WR)", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("EOD/Time Rate", 3200), dataCell("38.3%", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Days Active", 3200), dataCell("162 / 207", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("Avg Trades/Day", 3200), dataCell("3.83", 3080), dataCell("", 3080)] }),
                    new TableRow({ children: [dataCell("% Positive Days", 3200), dataCell("46.3%", 3080), dataCell("", 3080)] }),
                ]
            }),

            new Paragraph({ spacing: { before: 200 } }),

            // Robustness
            new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("Robustness Checks")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [4680, 2340, 2340],
                rows: [
                    new TableRow({ children: [headerCell("Check", 4680), headerCell("PF", 2340), headerCell("Status", 2340)] }),
                    new TableRow({ children: [dataCell("Full Universe", 4680), boldCell("1.10", 2340), dataCell("\u2713 Above 1.0", 2340)] }),
                    new TableRow({ children: [dataCell("Ex-Best-Day (2025-08-12, +16.2R)", 4680), boldCell("1.06", 2340), dataCell("\u2713 Above 1.0", 2340)] }),
                    new TableRow({ children: [dataCell("Ex-Top-Symbol (LULU, +8.8R)", 4680), boldCell("1.08", 2340), dataCell("\u2713 Above 1.0", 2340)] }),
                    new TableRow({ children: [dataCell("Cross-sample G1 (23 sym)", 4680), boldCell("1.22", 2340), dataCell("\u2713", 2340)] }),
                    new TableRow({ children: [dataCell("Cross-sample G2 (23 sym)", 4680), boldCell("1.16", 2340), dataCell("\u2713", 2340)] }),
                    new TableRow({ children: [dataCell("Cross-sample G3 (23 sym)", 4680), boldCell("1.04", 2340), dataCell("\u2713", 2340)] }),
                    new TableRow({ children: [dataCell("Cross-sample G4 (24 sym)", 4680), boldCell("0.77", 2340, "FFF3E0"), dataCell("\u2717 Below 1.0", 2340, "FFF3E0")] }),
                ]
            }),
            new Paragraph({ spacing: { before: 100, after: 200 }, children: [
                new TextRun({ text: "3 of 4 cross-sample groups profitable. G4 contains harder names (XLF, JNUG, BA, AAPL). Not driven by a single day or symbol.", italics: true, size: 20 })
            ] }),

            new Paragraph({ children: [new PageBreak()] }),

            // Monthly
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("3. Monthly Breakdown")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [2000, 1500, 1500, 2180, 2180],
                rows: [
                    new TableRow({ children: [headerCell("Month", 2000), headerCell("N", 1500), headerCell("R", 1500), headerCell("Cum R", 2180), headerCell("Status", 2180)] }),
                    ...[
                        ["2025-05", "21", "-1.3", "-1.3", "\u2717"],
                        ["2025-06", "56", "+3.1", "+1.8", "\u2713"],
                        ["2025-07", "59", "+8.3", "+10.1", "\u2713"],
                        ["2025-08", "106", "+14.3", "+24.4", "\u2713"],
                        ["2025-09", "71", "+8.7", "+33.1", "\u2713"],
                        ["2025-10", "62", "-5.9", "+27.2", "\u2717"],
                        ["2025-11", "35", "+3.2", "+30.4", "\u2713"],
                        ["2025-12", "43", "-7.7", "+22.8", "\u2717"],
                        ["2026-01", "79", "-8.8", "+13.9", "\u2717"],
                        ["2026-02", "69", "-3.1", "+10.9", "\u2717"],
                        ["2026-03", "20", "+22.4", "+33.3", "\u2713"],
                    ].map(([mo, n, r, cum, st]) => {
                        const fill = r.startsWith("+") ? "E8F5E9" : "FFEBEE";
                        return new TableRow({ children: [
                            dataCell(mo, 2000), dataCell(n, 1500), dataCell(r, 1500, fill), dataCell(cum, 2180), dataCell(st, 2180)
                        ] });
                    })
                ]
            }),
            new Paragraph({ spacing: { before: 100, after: 200 }, children: [
                new TextRun({ text: "6 positive months, 5 negative. Summer months (Jun\u2013Sep) strongest. Winter drawdown (Dec\u2013Feb) is the main concern. March 2026 is a partial month with outsized +22.4R.", italics: true, size: 20 })
            ] }),

            // Combined
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("4. Combined Stack Results")] }),
            new Paragraph({ spacing: { after: 120 }, children: [
                new TextRun("FLR was tested alongside the existing promoted stack (SC Long, BDR Short, EMA Pull Short) to check for interaction effects.")
            ] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [3500, 1200, 1200, 1730, 1730],
                rows: [
                    new TableRow({ children: [headerCell("Context", 3500), headerCell("N", 1200), headerCell("PF", 1200), headerCell("Exp(R)", 1730), headerCell("Total R", 1730)] }),
                    new TableRow({ children: [boldCell("FLR Standalone", 3500), dataCell("621", 1200), boldCell("1.10", 1200, "E8F5E9"), dataCell("+0.054", 1730), dataCell("+33.3", 1730)] }),
                    new TableRow({ children: [dataCell("FLR in Combined", 3500), dataCell("618", 1200), dataCell("1.09", 1200), dataCell("+0.045", 1730), dataCell("+28.0", 1730)] }),
                    new TableRow({ children: [boldCell("FLR in Capped (max 3)", 3500), dataCell("373", 1200), boldCell("1.15", 1200, "E8F5E9"), dataCell("+0.081", 1730), dataCell("+30.2", 1730)] }),
                    new TableRow({ children: [dataCell("Full Stack Unconstrained", 3500), dataCell("959", 1200), dataCell("0.94", 1200, "FFEBEE"), dataCell("-0.034", 1730), dataCell("-32.7", 1730)] }),
                    new TableRow({ children: [dataCell("Full Stack Capped (max 3)", 3500), dataCell("665", 1200), dataCell("0.92", 1200, "FFEBEE"), dataCell("-0.044", 1730), dataCell("-29.3", 1730)] }),
                ]
            }),
            new Paragraph({ spacing: { before: 100, after: 200 }, children: [
                new TextRun({ text: "Key finding: ", bold: true }),
                new TextRun("FLR improves under the cap (PF 1.15 vs 1.10 standalone) due to quality selection \u2014 when constrained, only the best entries get taken. The full stack is dragged down by SC Long (PF 0.52 in combined) which needs separate investigation."),
            ] }),

            // Risks
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("5. Risk Factors & Caveats")] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "Winter drawdown: ", bold: true }),
                new TextRun("Dec 2025 through Feb 2026 shows -19.6R cumulative loss. Strategy underperforms in low-volatility / range-bound markets."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "March 2026 outlier: ", bold: true }),
                new TextRun("+22.4R from 20 trades is suspiciously high. Only 11 days of data. Ex-best-day PF still holds at 1.06, but this month inflates total."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "G4 cross-sample weakness: ", bold: true }),
                new TextRun("1 of 4 groups (PF 0.77) is net negative. Names like AAPL, BA, JNUG are persistent losers \u2014 a symbol exclusion filter could help but risks overfitting."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "Thin edge: ", bold: true }),
                new TextRun("Expectancy of +0.054R is thin. Transaction costs or execution slippage beyond the 4bps model could erode the edge."),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 200 }, children: [
                new TextRun({ text: "Sample period: ", bold: true }),
                new TextRun("10 months (May 2025 \u2013 Mar 2026). Longer history needed for confidence."),
            ] }),

            // Optimization path
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("6. Optimization Path (for context, not applied)")] }),
            new Paragraph({ spacing: { after: 200 }, children: [
                new TextRun("Default config PF 0.72 \u2192 Parameter tuning PF 0.90 \u2192 3-bar turn PF 0.97 \u2192 "),
                new TextRun({ text: "4-bar turn + wider stop PF 1.10", bold: true }),
                new TextRun(". The 4-bar turn was the single biggest structural improvement. Wider stop (0.50 vs 0.40) reduced stop-out rate from 50.9% to 41.1% without sacrificing PF."),
            ] }),

            // Trade logs
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("7. Trade Log Files")] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "replay_flr_standalone.csv", bold: true }),
                new TextRun(" \u2014 621 trades, standalone FLR"),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: [
                new TextRun({ text: "replay_combined_with_flr.csv", bold: true }),
                new TextRun(" \u2014 959 trades, full stack unconstrained"),
            ] }),
            new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 200 }, children: [
                new TextRun({ text: "replay_combined_with_flr_capped.csv", bold: true }),
                new TextRun(" \u2014 665 trades, capped max 3 concurrent"),
            ] }),

            // Final verdict
            new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("8. Final Verdict")] }),
            new Table({
                width: { size: 9360, type: WidthType.DXA },
                columnWidths: [9360],
                rows: [new TableRow({ children: [new TableCell({
                    borders: { top: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               bottom: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               left: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" },
                               right: { style: BorderStyle.SINGLE, size: 3, color: "2E8B57" } },
                    shading: { fill: "E8F5E9", type: ShadingType.CLEAR },
                    width: { size: 9360, type: WidthType.DXA },
                    margins: { top: 120, bottom: 120, left: 200, right: 200 },
                    children: [
                        new Paragraph({ children: [
                            new TextRun({ text: "FL_MOMENTUM_REBUILD SURVIVES engine-native validation. ", bold: true, size: 22, font: "Arial" }),
                            new TextRun({ text: "PF 1.10, N=621, +33.3R over 10 months. All 5 promotion criteria pass. Edge survives ex-best-day (1.06) and ex-top-symbol (1.08) checks. Capped performance actually improves (PF 1.15). Recommend forward-testing with live market data.", size: 22, font: "Arial" }),
                        ] }),
                    ]
                })] })]
            }),
        ]
    }]
});

Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync("/sessions/inspiring-clever-meitner/mnt/alert_overlay/outputs/FLR_Validation_Memo.docx", buffer);
    console.log("FLR memo written.");
});
