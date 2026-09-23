# 智能辅具租赁目录

本项目保存社区智能辅具的产品分类、租赁价格和维护周期样例，为养老服务机构管理设备提供基础数据。

运行 `python -m unittest discover -s tests -v` 检查补贴比例边界。样例不包含老人身份和健康信息。

## 本地运行

测试命令：

```bash
python3 -m unittest discover -s tests
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
