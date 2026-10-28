# paper2poster-workflow-inspired
A workflow designed based on the inspiration of the "paper2poster" project and running on the dify platform

这是一个基于paper2poster的开源github项目，仿照其运行逻辑，借用内部部署好的大模型后上传的一个可以初步生成海报的小项目。目前的yml文件所生成的图片还是可以在布局上可以进行优化，所以，如果有时间的话会进行更新迭代。但大概率是比较忙哈~。

所上传的路由函数是在vscode连接远程服务器的大模型后，后端测试时所需要的文件，如果使用的话，端口可以修改成您的端口。

如果想要成功运行yml文件，需要在自己的dify平台上上传yml后，部署好相关大模型（可以自定义，只要api和key成立），然后上传相关的pdf论文就可以了。
该项目还有一名共同开发者，在未争取他同意之前，暂不写上其姓名。

开源项目链接：
https://github.com/Paper2Poster/Paper2Poster.git

以下是当时的设计思路以及一些笔记

一：P2P pipeline
1.解析pdf-转md-格式优化*标题+摘要+各段内容
   提取image*（用到docling） --难点    （lcm）http：uvicorn main:app --host 0.0.0.0 --port 44447 --reload
进度：解析pdf ok
转md？ no 上午试下
疑问：
优化要优化成什么样

2.过滤不必要的image--llm（image预处理，提取image信息后传入）(yzf)
3.生成大纲，包含海报内容的逻辑结构和初步的布局意图（相对比例）
4.生成海报布局-从自带的多篇数据集学习并推断布局分配，没用llm
   精细化标题布局
   将整个布局生成阶段的详细结果，包括海报的尺寸信息和所有计算出的元素位置/尺寸，保存到文件
5.根据之前确定的布局来总结生成各部分文字
6.应用海报格式
7.生成ppt
8.ppt转image

二：ai思路
使用 Dify 工作流实现 Paper2Poster 的功能是一个非常有趣且具有挑战性的想法！Dify 的工作流能力确实可以帮助你串联起大部分步骤。下面我将根据你总结的流程，一步步分析如何在 Dify 中实现，并指出可能的难点和解决方案。

流程
1.	解析 PDF - 转 MD - 格式优化（标题 + 摘要 + 各段内容）
•	Dify 节点：
￮	Start (开始) 节点： 配置一个 文件变量，允许用户上传 PDF 文件。
￮	Document Extractor (文档提取器) 节点： 这个节点是 Dify 原生支持的，可以将 PDF 内容提取为文本。
￮	Code (代码) 节点 / LLM (大语言模型) 节点：
▪	转 MD 和格式优化： Document Extractor 提取的文本通常是纯文本。你需要一个后续步骤来将其转换为 Markdown 格式并进行优化。 
•	Code 节点 (推荐): 如果你有现成的 Python 库 (如 pandoc 或自定义脚本) 可以将纯文本转换为 Markdown 并进行格式优化，这是一个很好的选择。你可以在 Code 节点中编写 Python 代码来调用这些库，并处理提取的文本。
•	LLM 节点： 你也可以尝试使用一个强大的 LLM (如 GPT-4o 或 Claude 3 Opus) 来进行 Markdown 转换和格式优化。在 LLM 节点的 Prompt 中清晰地指示它将文本转换为 Markdown 格式，并提取标题、摘要和各段内容。但要注意，LLM 在格式化方面的稳定性可能不如代码。
•	难点：提取 Image (用到 Docling)
￮	Dify 现有能力： Dify 的 Document Extractor 节点主要提取文本内容，目前对图片提取的支持有限。虽然 Dify 支持图片上传并让 LLM 进行视觉识别，但这通常是针对单独上传的图片，而不是从 PDF 中“解析”出来的图片。
￮	解决方案：
▪	外部服务 / Docling 集成 (推荐): 这是最直接的解决方案。Dify 的工作流可以通过 HTTP Request (HTTP 请求) 节点 调用外部 API。 
•	你需要搭建一个服务来运行 Docling（或类似的 PDF 解析库），当 Dify 工作流调用时，将 PDF 文件作为输入发送给这个服务。
•	这个外部服务负责使用 Docling 解析 PDF，提取图片，并将图片的 URL 或 base64 编码返回给 Dify。
•	如果图片数量多，可以考虑将图片上传到图床，Dify 接收图片 URL。
▪	Code 节点内的复杂逻辑 (挑战大): 如果 Docling 是一个 Python 库，理论上你可以在 Dify 的 Code 节点中直接运行它。但 Dify 的 Code 节点有运行环境和依赖限制，你可能需要确保 Docling 及其所有依赖都能在 Dify 的沙盒环境中安装和运行，这可能比较困难。
2.	过滤不必要的 Image -- LLM (Image 预处理，提取 Image 信息后传入)
•	Dify 节点：
￮	LLM (大语言模型) 节点 (Vision 能力): Dify 的 LLM 节点支持 Vision 能力。 
▪	输入： 将步骤 1 中提取到的图片（通过 URL 或 base64 编码传入）作为 LLM 节点的 Vision 输入。
▪	Prompt： 设计一个 Prompt，要求 LLM 分析图片内容，并判断图片是否“不必要”（例如，纯装饰性图片、低质量图片、与内容不相关的图片等）。让 LLM 输出需要保留的图片列表或过滤掉的图片列表。
▪	Image 预处理： 如果图片信息复杂，可以在 LLM 处理前，用另一个 Code 节点对图片进行一些预处理，例如提取图片尺寸、文件类型等元数据，作为辅助信息传给 LLM。

3.	生成大纲，包含海报内容的逻辑结构和初步的布局意图（相对比例）
•	Dify 节点：
￮	LLM (大语言模型) 节点：
▪	输入： 结合步骤 1 优化后的 Markdown 文本内容（标题、摘要、各段内容）和步骤 2 过滤后的图片信息。
▪	Prompt： 清晰地指示 LLM 根据这些内容生成海报的大纲。大纲应包含： 
•	海报内容的逻辑结构（例如：引言、方法、结果、讨论、结论等）。
•	初步的布局意图（例如：标题区占比 10%，摘要区 15%，主要内容区 60%，图片区 15%，图片和文本的相对位置关系）。可以用 JSON 格式或其他结构化格式输出，方便后续节点解析。
4.	生成海报布局 - 从自带的多篇数据集学习并推断布局分配，没用 LLM
•	Dify 节点：
￮	Code (代码) 节点 / 外部服务 (推荐):
▪	由于你提到这部分不使用 LLM，而是从数据集学习和推断布局，这意味着你需要一个专门的布局算法。
▪	外部服务 (推荐): 搭建一个独立的布局服务。这个服务接收步骤 3 生成的大纲和布局意图，结合你自己的海报布局数据集，生成精确的海报尺寸、所有元素的精确位置和尺寸信息，并将其保存到文件（例如 JSON 文件）。Dify 通过 HTTP Request 节点调用该服务。
▪	Code 节点： 如果你的布局算法逻辑可以纯 Python 实现，并且没有复杂的外部依赖，可以尝试放在 Code 节点中。但考虑到“从自带的多篇数据集学习并推断布局分配”可能涉及模型推理，Code 节点的性能和环境可能不足以支持。

5.	根据之前确定的布局来总结生成各部分文字
•	Dify 节点：
￮	Iterator (迭代器) 节点 (可选，如果布局细分多部分): 如果海报布局将文本区域细分为多个小部分（例如，结果部分的每个图表对应一段文字），可以使用迭代器遍历这些部分。
￮	LLM (大语言模型) 节点：
▪	输入： 步骤 1 优化后的原始文本内容，以及步骤 4 确定的海报布局信息（每个文本区域的大小、位置和对应的内容主题）。
▪	Prompt： 指示 LLM 根据海报布局中每个文本框的约束（例如，字数限制、主题要求）来总结和生成相应的文字内容。这可能需要多次调用 LLM，每次针对海报的一个特定文本区域进行内容生成。

6.	应用海报格式
•	Dify 节点：
￮	Code (代码) 节点 / 外部服务 (推荐):
▪	这一步涉及将生成的内容（文本和图片）按照精确的布局信息应用到海报模板上，并进行排版和视觉样式调整。这通常需要专业的图形处理库或排版引擎。
▪	外部服务 (推荐): 搭建一个海报生成服务，接收步骤 4 的布局数据和步骤 5 生成的文本内容，以及过滤后的图片，然后生成海报的图像文件（例如 PNG, JPG）。这个服务可以使用如 Pillow (PIL), Cairo, FPDF, 或更复杂的排版工具 (如 LaTeX 结合 Python 脚本) 来实现。
▪	Code 节点： 同样，如果你的格式应用逻辑可以用纯 Python 库实现，可以尝试放在 Code 节点。

7.	生成 PPT
•	Dify 节点：
￮	Code (代码) 节点 / 外部服务 (推荐):
▪	外部服务 (推荐): 使用 python-pptx (Python 库) 或其他专门的工具来生成 PPT 文件。这个服务接收海报的文本内容、图片和布局信息，然后将其组织成 PPT 的幻灯片。Dify 通过 HTTP Request 节点调用。
▪	Code 节点： python-pptx 可以在 Code 节点中运行，这可能是相对容易实现的一步，因为 PPT 生成通常不涉及复杂的图形渲染，更多是内容组织。

8.	PPT 转 Image
•	Dify 节点：
￮	Code (代码) 节点 / 外部服务 (推荐):
▪	外部服务 (推荐): 将 PPT 转换为图像通常需要像 LibreOffice 或 Microsoft PowerPoint 这样的软件，并通过命令行接口或自动化脚本进行转换。你可以搭建一个服务来执行这个操作。
▪	Code 节点 (挑战大): 直接在 Dify 的 Code 节点中进行 PPT 到图片的转换非常困难，因为它通常需要桌面软件环境。

整体工作流设计思路：
1.	Start (开始) 节点： 用户上传 PDF 文件。
2.	Document Extractor (文档提取器) 节点： 提取 PDF 文本。
3.	Code (代码) 节点 / HTTP Request (HTTP 请求) 节点 (Docling 服务)：
￮	Code 节点： 处理 Document Extractor 的输出，进行 Markdown 转换和初步格式优化。
￮	HTTP Request 节点： 将 PDF 文件发送到外部 Docling 服务，获取图片列表。
4.	LLM (大语言模型) 节点 (Vision)： 过滤图片。
5.	LLM (大语言模型) 节点： 生成大纲和初步布局意图。
6.	HTTP Request (HTTP 请求) 节点 (布局服务)： 调用外部布局服务，生成精确布局数据。
7.	LLM (大语言模型) 节点 (多次调用或迭代器): 根据布局总结生成各部分文字。
8.	HTTP Request (HTTP 请求) 节点 (海报生成服务)： 生成海报图片。
9.	Code (代码) 节点 / HTTP Request (HTTP 请求) 节点 (PPT 生成服务)： 生成 PPT 文件。
10.	HTTP Request (HTTP 请求) 节点 (PPT 转图片服务)： 将 PPT 转换为图片。
11.	End (结束) 节点： 将最终生成的海报图片和 PPT 文件作为输出返回给用户。


1.github下整个文件夹-解压 用scp在本地终端上传到你的对应的/data0/name(你的名字）下
2.登录Ubuntu 28服务器 cd到这个文件夹-docker
3.根据readme的社区版的说明（quick start）下东西：
cd docker
cp .env.example .env
docker compose up -d

确保自己的/.bashrc有
export http_proxy="http://10.239.121.46:3128"
export https_proxy="http://10.239.121.46:3128"

三：流程
1.
初始化： poster_gen_pipeline.py 开始执行，解析命令行参数，设置海报名称和模型配置。
原始内容解析 (在 parse_raw.py 中实现)：
•	parse_raw 函数被调用。
•	PDF文件被 docling（或回退到 marker）转换为Markdown格式的文本。
•	清理Markdown文本。
•	使用Jinja2模板构建一个提示，其中包含清理后的Markdown文本。
•	ChatAgent 将此提示发送给AI模型（LLM）。
•	从AI模型接收响应，并提取结构化的JSON内容，其中包含海报的各个部分（标题、章节等）。
•	对返回的JSON进行验证和潜在的章节筛选。
•	将解析出的内容保存到 contents 目录下的JSON文件中。
•	记录此步骤的输入和输出 token 消耗。
图像和表格生成 (在 parse_raw.py 中实现)：
•	gen_image_and_table 函数被调用。
•	遍历已转换的文档，提取所有页面图像、图表图像和表格图像。
•	将这些图像保存到以海报名称命名的目录中。
•	提取图像和表格的元数据（标题、路径、尺寸等），并保存到 _images.json 和 _tables.json 文件中。
后续步骤 (在 poster_gen_pipeline.py 中调用)：
•	filter_image_table：根据海报内容筛选出相关的图像和表格。
•	gen_outline_layout：生成海报的大纲和布局。
•	gen_poster_content：根据大纲和布局生成海报的详细文本内容。
•	fill_poster_content：将生成的内容填充到海报模板中。
•	stylize_poster：对海报进行样式化。
•	deoverflow：处理内容溢出问题。
•	poster_apply_theme (可选)：如果指定了模板路径，则应用主题。
