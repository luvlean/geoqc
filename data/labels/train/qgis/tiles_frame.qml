<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis styleCategories="Symbology" version="3.34.0">
  <renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">
    <rules key="{30f00ffb-f9c5-4184-80cd-a121cbced83a}">
      <rule key="{55cee1d1-aca1-410d-b6de-3dd29171fee4}" symbol="0" label="тайл" filter="coalesce(&quot;recheck&quot;, false) = false"/>
      <rule key="{b29918f3-3527-4da7-a0dc-135f55a9f22c}" symbol="1" label="тайл для повторной разметки (recheck)" filter="&quot;recheck&quot; = true"/>
    </rules>
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{2711b4b0-97f0-4bcd-8b38-c49a9e601f79}">
          <Option type="Map">
            <Option type="QString" name="border_width_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="color" value="255,255,0,0"/>
            <Option type="QString" name="joinstyle" value="miter"/>
            <Option type="QString" name="offset" value="0,0"/>
            <Option type="QString" name="offset_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="offset_unit" value="MM"/>
            <Option type="QString" name="outline_color" value="255,220,0,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="0.9"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="solid"/>
          </Option>
        </layer>
      </symbol>
      <symbol type="fill" name="1" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{41de43c2-da73-42a5-a567-882ebb3db53a}">
          <Option type="Map">
            <Option type="QString" name="border_width_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="color" value="0,255,255,0"/>
            <Option type="QString" name="joinstyle" value="miter"/>
            <Option type="QString" name="offset" value="0,0"/>
            <Option type="QString" name="offset_map_unit_scale" value="3x:0,0,0,0,0,0"/>
            <Option type="QString" name="offset_unit" value="MM"/>
            <Option type="QString" name="outline_color" value="0,220,255,255"/>
            <Option type="QString" name="outline_style" value="solid"/>
            <Option type="QString" name="outline_width" value="1.2"/>
            <Option type="QString" name="outline_width_unit" value="MM"/>
            <Option type="QString" name="style" value="solid"/>
          </Option>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <layerGeometryType>2</layerGeometryType>
</qgis>
