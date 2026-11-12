# 训练数据

每次训练产出一个**不可变目录**：

```
训练数据/<模型名>/<run_id>/
    manifest.json                     本次训练的参数、数据指纹、指标、产物 sha256
    artifacts/predictions.csv         三个集的逐条预测
    artifacts/feature_importance.csv  特征重要性
    artifacts/model.json              模型本体（不入库）
    logs/train.log                    训练日志
```

## 本目录的内容不进 git

- 单次约 66 MB，且完全可由「划分 + 训练」重生成；
- 目录名与文件名已含模型名、特征组、run 短码，看一眼就知道是哪次实验。

仓库里只保留了骨架（`XGboost/.gitkeep`），所以刚 clone 下来这里是空的。
填充它的办法：

```bat
:: 方式一：重新训练（推荐，10 秒左右）
集成操作\开始训练.bat

:: 方式二：已有产物（例如从别处拷贝了整个 run 目录）放进本目录后
cd 模型\管理器
python cli.py record      :: 收进台账
python cli.py chart       :: 出图
python cli.py report      :: 出报告
```

## 想知道"历史上都跑过什么"

台账 `模型\管理器\catalog.sqlite` 同样是本地产物（可重建），不入库。
要留痕可以做一份可 diff 的摘要：

```bat
cd 模型\管理器
python cli.py top --all
```

或者把 `模型\管理器\榜单.csv` 这类导出文件提交（几 KB，便于逐次对比）。
