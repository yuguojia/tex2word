# tex2word 生成的 Word 样式一览

样式大致分为 **①段落样式（命名样式）**、**②字符样式（命名样式）**、**③run 级别的直接格式（无命名样式）** 三类。样式定义本体在 `src/tex2word/templates/styles.xml`，应用逻辑在 `src/tex2word/backend/document.py` 和 `frontend/parser.py` 中。

## ① 段落样式（命名样式 / `w:pStyle`）

| Word 样式 ID | 显示名 | 格式特征 | 触发的 LaTeX 指令 |
|---|---|---|---|
| `Title` | Title | 居中・加粗・26pt | `\title{…}` + `\maketitle` |
| `Subtitle` | Subtitle | 居中・斜体・14pt | `\author`、`\date`、所属/元信息（`document.py:167-175`） |
| `Heading1` | heading 1 | 加粗 16pt | `\section`（article）/ `\chapter`（book・report） |
| `Heading2` | heading 2 | 加粗 14pt | `\subsection` / book 中的 `\section` |
| `Heading3` | heading 3 | 加粗 13pt | `\subsubsection` / book 中的 `\subsection` |
| `Heading4` | heading 4 | 加粗斜体 12pt | `\paragraph` / `\subparagraph` |
| `Heading5` | heading 5 | 加粗 11pt | book/report 更深层级的回退 |
| `Caption` | caption | 居中・斜体 10pt | `\caption{…}`（figure / table / algorithm / subfigure） |
| `Quote` | Quote | 左右缩进・斜体 | `quote` / `quotation` 环境、`csquotes` 的块引用、`epigraph` |
| `SourceCode` | Source Code | 灰底・Consolas 10pt | `verbatim` / `lstlisting` / `minted` / `\lstinputlisting` |
| `Abstract` | Abstract | 左右缩进 10pt | `abstract` 环境 |
| `Bibliography` | Bibliography | 悬挂缩进 | `thebibliography` / `\printbibliography` / `.bbl` 的各文献条目 |
| `FootnoteText` | footnote text | 10pt・紧凑行距 | `\footnote{…}` / `\endnote{…}` 的正文（脚注窗格侧） |

> 补充：标题的层级对应在 `parser.py:38-46` 的 `_SECTION_LEVELS` / `_SECTION_LEVELS_BOOK` 中确定。`\section*` 等带星号形式会变为无编号，但样式本身相同。`\part`、`\appendix` 下的标题，以及使用 `--reference-doc` 模板时，可通过 `appendix_style_ids` / `part_style_id` 等替换为其他样式 ID（`document.py:229-236`）。

## ② 字符样式（命名样式 / `w:rStyle`）

| Word 样式 ID | 显示名 | 触发指令 |
|---|---|---|
| `Hyperlink` | Hyperlink | `\href`、`\url`、`hyperref` 的链接（蓝色・下划线） |
| `FootnoteReference` | footnote reference | 脚注/尾注的引用标记（上标编号） |

## ③ run 级别的直接格式（不是命名样式，而是直接赋予 `w:rPr`）

这些通过 `parser.py:58-77` 的对应表转换为 IR 的 `Emphasis(kind=…)`，再由 `document.py:1260-1304` 的 `_run()` 变成 Word 的 run 属性。

| 格式（Word 的 run 属性） | 触发的 LaTeX |
|---|---|
| 加粗 `w:b` | `\textbf`、`{\bfseries …}`、`{\bf …}` |
| 斜体 `w:i` | `\textit`、`\emph`、`\textsl`、`{\itshape}`、`{\em}`、`{\it}` |
| 下划线 `w:u` | `\underline`、`ulem` 的 `\uline` / `\uuline` |
| 删除线 `w:strike` | `ulem` 的 `\sout` / `\st` / `\xout` |
| 小型大写 `w:smallCaps` | `\textsc`、`{\scshape}`、`{\sc}` |
| 等宽字体（Consolas）`w:rFonts` | `\texttt`、`{\ttfamily}`、`{\tt}`、`\verb|…|`、`\lstinline` |
| 上标 `w:vertAlign=superscript` | `\textsuperscript` |
| 下标 `w:vertAlign=subscript` | `\textsubscript` |
| 文字颜色 `w:color` | `\textcolor`、`\color{…}`、`xcolor` |
| 荧光高亮 `w:highlight`／底纹 `w:shd` | `soul` 的 `\hl`、`\colorbox` / `\fcolorbox` |
| 恢复直立（取消格式） | `\textnormal`、`\textrm`、`\textsf`、`\textmd`、`\textup`、`\mbox` |

## 不使用命名样式的元素（直接格式化）

- **列表**（`itemize` / `enumerate` / `description`）：不使用 `ListParagraph` 样式，而是直接赋予 `w:numPr`（编号定义）＋缩进（`document.py:242-411`）。
- **表格**（`tabular` / `longtable` / `booktabs`）：用 `w:tblBorders` 直接绘制边框。`\multicolumn`→单元格合并，`\multirow`→纵向合并。
- **公式**：不是命名样式，而是作为原生 OMML（`w:oMath`）嵌入。
- **定理类**（`theorem` / `lemma` / `definition` / `proof` …）：不用段落样式，而是在开头以 run 格式加上加粗的带编号引导文字（`proof` 为斜体＋QED 标记）（`document.py:948-995`）。
- **题注编号・交叉引用**：生成为 `SEQ` / `REF` / `PAGEREF` / `STYLEREF` 等 Word 域（field）。

---

总而言之，**命名样式共 13 个段落样式＋2 个字符样式**，对应标题、题目、题注、引用、代码、摘要、参考文献、脚注等「结构」。而 `\textbf` 等 **文字装饰不是命名样式，而是作为 run 的直接格式**输出。
