<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis styleCategories="Symbology" version="3.34.0">
  <renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">
    <rules key="{7b741821-cdcd-4829-bbe1-920a319f542a}">
      <rule key="{27f8ce64-8440-4637-9742-91cce9e5943b}" symbol="0" label="не проверено (verified=false)" filter="coalesce(&quot;verified&quot;, false) = false"/>
      <rule key="{2a454b5c-22c1-4b1b-b107-c6eedaee3e6e}" symbol="1" label="проверено (verified=true)" filter="&quot;verified&quot; = true"/>
    </rules>
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{0fc96d98-bbdf-4b3e-88e6-f97016bf2d6d}">
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
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{caa7eebf-7145-45dc-bd8d-a1de28c15265}">
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
