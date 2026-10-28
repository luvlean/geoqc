<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis styleCategories="Symbology" version="3.34.0">
  <renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">
    <rules key="{cfbbd271-36a0-4fee-b568-41840f77bbcd}">
      <rule key="{1b29ff1d-66f1-4ec1-b9c4-a2cbb32fb113}" symbol="0" label="не проверено (verified=false)" filter="coalesce(&quot;verified&quot;, false) = false"/>
      <rule key="{ee0f7c50-926a-44b6-81ff-36393873d619}" symbol="1" label="проверено (verified=true)" filter="&quot;verified&quot; = true"/>
    </rules>
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{c93c7946-62a0-4275-ba69-3a7694714999}">
          <Option type="Map">
            <Option type="QString" name="border_width_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="color" value="255,140,0,70"/>
            <Option type="QString" name="joinstyle" value="miter"/>
            <Option type="QString" name="offset" value="0,0"/>
            <Option type="QString" name="offset_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="offset_unit" value="MM"/>
            <Option type="QString" name="outline_color" value="255,40,0,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="0.5"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="solid"/>
          </Option>
        </layer>
      </symbol>
      <symbol type="fill" name="1" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{33a9f3e4-0aaf-40ab-b7d4-35acff3f176a}">
          <Option type="Map">
            <Option type="QString" name="border_width_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="color" value="0,200,80,30"/>
            <Option type="QString" name="joinstyle" value="miter"/>
            <Option type="QString" name="offset" value="0,0"/>
            <Option type="QString" name="offset_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="offset_unit" value="MM"/>
            <Option type="QString" name="outline_color" value="0,170,60,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="0.5"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="solid"/>
          </Option>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <layerGeometryType>2</layerGeometryType>
</qgis>
