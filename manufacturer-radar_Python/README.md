# 厂商雷达 v1.2

Python / Tkinter Windows 桌面软件。输入产品或行业，通过六个搜索引擎、B2B公开页、展商/政府名录寻找候选，也支持地图官方接口、名单导入及浏览器辅助。实际进入网页识别厂家主体与主营后才以厂家卡片显示。原始搜索链接只存在采集记录中，未读取或识别失败不能作为厂家入库。详见 [多渠道说明](MULTISOURCE.md)。

## 运行

便携版解压整个文件夹，双击 `ManufacturerRadar.exe`。适用 Windows 10/11 64 位，无需安装 Python。

源码推荐 Python 3.11+，本次使用 Python 3.14.8：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

某些 Windows Python 3.14 把 Tcl/Tk 脚本放在 `tcl/lib*.zip`，启动器会展开到项目 `.runtime-tcl`，构建脚本也会处理这种布局。

## 收录过程

1. 产品/行业词自动补足“厂家”搜索意图；六引擎并行，各最多前 3 页，只保存候选与原始出处。
2. 实际读取候选详情页，跟随有限普通 HTTP/静态网页跳转。
3. 从详情页的主体、公司简介、产品、供应商资料或联系方式识别厂家全称与相关主营。
4. 排除百科、资讯、问答、泛目录、主体不明及无关业务页面。名称和主营不能由搜索摘要冒充详情证据。
5. 合并同一厂家，保留原始链接和实际厂家页；有公开电话则附电话和原文出处。
6. 展示主营、地区、电话、评分、负面待核实线索和收录依据。失败或未处理的URL仅保留在采集记录。

默认120秒，最多访问120个不同候选URL、每家可选最多2个联系页。负面补查最多12家已识别厂家、每家第1页。可继续处理未识别候选。

## 构建与验证

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Python .\.venv\Scripts\python.exe
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python main.py --self-test --report self-test.json
```

输出目录 `dist/ManufacturerRadar/` 整体打 ZIP 分发，不可只复制EXE。`--self-test --online` 额外执行真实联网识别，可能受验证码和网站限制。`tools/live_smoke.py` 保存候选处理原因和每家厂家页面证据，便于审查实际识别结果。

模块：`providers.py` 六引擎；`discovery.py` B2B与名录；`ingestion.py` 本地名单；`map_sources.py` 官方地图接口；`browser_assist.py` 专用浏览器；`network.py` 有界网络与跳转；`qualification.py` 实际页面厂家准入；`extraction.py` 文本、电话和实体合并；`pipeline.py` 发现→访问→识别→收录；`scoring.py` 资料评分；`storage.py` 历史和导出；`gui*.py` 桌面界面。

页面识别不是工商实名核验；评分不是信用评级。广告显示和网页内容因地区、时间而变化。软件不保证固定收录数量，不提供验证码规避、付费数据库或资质认证。
