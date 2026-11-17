# pairwise-atlas-packer

我们的 UI 图集打包器现在是一个 300 行的万能函数，加 padding 后相邻图会 bleeding，旋转 90° 的 sprite UV 全错，非 2 的幂尺寸直接被裁掉；packer/atlas.py 里排序、去重、分页、9-slice 信息全混在一起，改一个参数就影响所有页面；透明边缘没有 extrude，缩放时出现黑边；重复命名的贴图被重复打包；超大图没有提前报错，直接生成越界 UV。packer/atlas.py 是核心，tests/test_atlas.py 15个红。要修的是：按面积/高度排序并做 shelf packing；padding 独立于图块尺寸；旋转后交换宽高并正确计算 UV；零面积忽略、超大图报错；同名去重；分页超过上限报错；保留稳定的输入顺序和 page 索引。

## 运行

```bash
python -m pytest tests -q
```

初始状态：15 个用例失败。修复实现使全部用例通过，不要修改 `tests/` 下的测试。
