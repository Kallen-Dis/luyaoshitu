# 水域边界数据接口

后端默认读取 `data/geography/domain.geojson`；可通过进程环境变量 `GEOGRAPHIC_DOMAIN_PATH` 指定其他文件。无需修改 Referer 白名单。

实时步行计算在没有本地文件时默认自动获取 OSM 水域，并在 `.cache/osm-water/` 缓存原始响应。此目录仍可放入人工核验过的本地边界，以优先使用它。不要将下面的示例当作真实地理数据安装。

输入为 GeoJSON FeatureCollection，并带以下顶层 `properties`：

```json
{
  "type": "FeatureCollection",
  "properties": {
    "coord_sys": "bd09",
    "source": "实际数据来源及许可说明",
    "version": "数据日期或版本",
    "coverage": {"type": "Polygon", "coordinates": []}
  },
  "features": []
}
```

`coverage` 必须是真实的、已完整处理的范围多边形，不能用全国矩形代替局部数据的覆盖范围。覆盖声明必须包住整个分析窗口，否则返回 incomplete，不能将缺失数据视为没有水。`coord_sys` 仅接受 bd09，未知坐标系拒绝加载。

每个 feature 的 `properties.kind`：

- `water`：海域、湖泊、河流等明确水面，多边形可带孔洞。
- `walkable_deck`：经数据或现场核实允许步行的桥面/平台多边形。普通道路折线不能自动扩成桥面。桥面只抵消它实际覆盖的水域部分。

数据准备时需要核对海岸线和内陆水域覆盖、坐标转换、桥面可步行性、版本与来源许可。没有通过这些检查的文件不得用作正式水域排除依据。源数据的精度与更新周期应在 source/version 对应说明中记录。

加载成功后，射线采样跳过已知水面；正式射线几何、内圈、设施覆盖与网格按约束范围统计；融合网格沿水域边界分割，跨水单元不参与求解。水域孔洞不会被当成需要路线超时补测的陆地孔洞。

OSM 自动模式按约0.02度地区网格和分析半径生成稳定查询窗口，获取海岸线、闭合水面及多边形关系。海岸线依照陆左水右规则重建；湖泊和河流保留内环；没有内环的关系合法。断裂、自交、关系缺失、服务超时与部分响应均不作为“没有水”。没有海岸线返回只表示查询未返回海岸线，不能证明该处一定为陆地。

有效缓存30天，失败冷却5分钟；同一进程串行请求且间隔至少10秒；响应上限16MiB，单次请求有30秒总时限。默认实例为 OSM 官方列表中的 VK Maps Overpass，可用 `OSM_OVERPASS_URL` 指定其他 HTTPS Overpass 实例；设置 `OSM_WATER_AUTO=0` 可关闭在线获取。以上可用进程环境变量配置，无需改变 Referer 白名单。历史快照不会自动重算。

原始 OSM 坐标为WGS84，本地通过近似WGS84→GCJ02→BD09换算。结果记录 `alignment_verified=false`，换算往返测试不能代替外部精度核验。OSM可能漏收、岸线可能过时，普通桥梁折线不会扩成桥面。可信步行终点与水域冲突时拒绝融合候选，要求核实桥面或岸线，不能静默忽略路线。

来源与许可：© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)，ODbL 1.0。海域重建规则见[海岸线说明](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dcoastline)，公共实例使用见[Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)。项目界面显示来源、版本和近似换算说明。缓存不是全国完整水域库；任意地点都执行同一获取规则，但受OSM收录、公共服务可用性与桥面数据完整性限制。
