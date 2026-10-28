<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis styleCategories="Symbology" version="3.34.0">
  <renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0" enableorderby="0" referencescale="-1">
    <rules key="{8a518102-f11f-4a8b-932d-8bcfbdd10d8c}">
      <rule key="{b1b1d195-e46b-44f4-bb17-b04a775966ee}" symbol="0" label="тайл" filter="coalesce(&quot;recheck&quot;, false) = false"/>
      <rule key="{cd31e984-6f7d-4ffb-aac9-0dc276e4b778}" symbol="1" label="тайл для повторной разметки (recheck)" filter="&quot;recheck&quot; = true"/>
    </rules>
    <symbols>
      <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0" is_animated="0" frame_rate="10">
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{8e42749c-2e88-4ad7-9109-3b5aefcd956b}">
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
        <layer class="SimpleFill" enabled="1" locked="0" pass="0" id="{f94b419e-8c96-4567-973b-9771a09b1aa9}">
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
