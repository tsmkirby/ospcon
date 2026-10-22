# OSP Connectivity

QGIS plugin for outside plant cabling. It links cables, panels, ducts, and devices from the attributes already on the map, traces a circuit back to an IDF, and splits a fiber cable where a splice enclosure is added.

It runs on Windows and macOS with the Python and Qt libraries that ship with QGIS. No extra Python packages are required.

It works with two project shapes:

- A project that already has patch panels, splice tables, strand tables, and QGIS relations.
- A project that keeps the same facts as text on the features and does not have those related tables.

QGIS 3.34 or newer is required. Developed against QGIS 3.44.

## Install

The plugin folder has to stay named `osp_connectivity`, with `metadata.txt` directly inside it. QGIS loads the plugin from that package name. Close QGIS before copying the folder in, then start QGIS and enable **OSP Connectivity** under **Plugins → Manage and Install Plugins**.

If you use a named QGIS profile instead of the default profile, copy the folder into that profile's `python/plugins` directory.

### Windows

1. Copy the `osp_connectivity` folder to `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`.
2. Paste that path into File Explorer's address bar. It is usually `C:\Users\<your name>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins`.
3. Confirm the plugins folder contains `osp_connectivity\metadata.txt`.
4. Start QGIS and enable the plugin.

Copy the folder rather than making a shortcut. QGIS reads the plugin from that directory.

### macOS

1. Copy or symlink the `osp_connectivity` folder into `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`.
2. Start QGIS and enable the plugin.

## Toolbar

The **OSP Connectivity** toolbar uses compact square icons. Hover over an icon to see its name; the same actions are available under **Plugins → OSP Connectivity**. The toolbar has these actions. Results open in the **OSP Report** dock, which can copy the text, export a spreadsheet, print a circuit card, or clear the map highlight.

| Action | What it does |
| --- | --- |
| Build associations | Reads the open project and fills links that the attributes already imply. |
| Associate selection | Builds one circuit from the features you have selected, then asks for the panel and port. Handholes the cable passes through are included. |
| Modify associations | Select one device to change its panel, port, and handholes, or select an enclosure to open Splice Editor. |
| Splice Editor | Select a fiber enclosure to edit all its cable pairs and fiber ranges. |
| Trace selection | Follows the selected device, cable, or IDF and highlights the path. |
| Add fiber enclosure | Click a fiber cable. The cable is split and you choose which strands are spliced. |
| Add patch panel | Click an IDF. A copper cable gets a copper panel and a fiber cable gets a fiber panel, with the port and ends recorded in the dialog. A new panel asks for its port count and is stored on that IDF. |
| View patch panel | Click an IDF, then choose one of its patch panels. Every port is shown. Click a port to see the rear cable and any switch patch on it. |
| View switch | Click an IDF, then choose one of its switches. Patched ports show the panel and port. |
| Add switch | Click an IDF. Creates the switch, with ports such as `gi1/0/1`, on that IDF. |
| Patching | Cross-connect an open patch-panel port to an open switch port at the same IDF. |
| Add device | Click the map. Choose the device layer and a unique name. A cable or pole under the click is written onto the device before the attribute dialog, and those fields are left off the form. |
| Add cable | Draw a line. Snapping and tracing follow the line tool, so the path can follow a duct bank. Ends, panels, ports, and enclosures the line meets are written before the attribute dialog, and those fields are left off the form. |
| Set snap tolerance | Sets the distance used for duct chaining, enclosure clicks, and panel clicks. |

### Build associations

Use this when the ends are already written on the features. For each cable, device, IDF, pole, and duct the plugin can recognize, it writes the matching link and leaves a note when an end cannot be resolved.

It never overwrites a field that already has a value, including placeholders such as `unk`. Duplicate cable ids are reported and are not merged. The first cable keeps the device, panel, and port when more than one cable claims them.

### Associate selection

Select the pieces of one path, then run **Associate selection**. A typical copper selection is an AP, its cable, the duct-bank segments it follows, the pole it is mounted on, and the IDF. The dialog asks for:

- Which end is the device and which end is the IDF. A and Z are labels, not a direction of signal. The dialog follows the pattern already used on that layer.
- The patch panel and port. The list shows the panels already associated with the IDF selected in the dialog, and it changes when you pick a different IDF. You can type a new panel id. When you do, that panel is created on the selected IDF and linked to it.
- For fiber, the strand numbers. Ports may be a single number or a range such as `1-2`.

The dialog will not save when no cable is selected, more than one device is selected, more than two IDFs are selected, copper and fiber are mixed, or the chosen port is already occupied.

Handholes the selected cables pass through are listed and checked. Each one is shown by its access id, or by its access name when the access id is blank. Uncheck one to leave it out. The ones you keep are stored with the cable, in order along the run.

### Modify associations

Select one AP, camera, jack, or POS, then run **Modify associations**. The dialog lists that device’s cable, IDF, panel, port, and handholes. Change the panel or the port, check or uncheck handholes, and save. A panel name that is not already a feature is created on the IDF. This is an explicit edit, so the panel and port you enter replace the ones stored for that device.

Select one fiber enclosure instead, and the same tool opens **Splice Editor**.

### Splice Editor

Select a fiber enclosure, then click **Splice Editor** (or **Modify associations**). The table loads **all** splice connections in the enclosure, including express fibers and connections between different cable pairs. Contiguous fibers appear as ranges, such as cable A `47-48` → cable C `1-2`.

- Add a row, change either cable or fiber range, or select rows and delete them. Both fiber ranges must have the same length. A deleted connection leaves its fibers available.
- Choose **FUSION**, **EXPRESS**, **MECHANICAL**, or **OPEN**. Express fibers retain the same numbers on both cables. OPEN records an explicitly open range; delete that row before assigning those fibers to another connection.
- The cable inventory shows total fibers, reserved fibers, and available ranges. It includes cables stored in the enclosure relations and new fiber cables whose endpoints reach the enclosure within the snap tolerance. Enter a fiber count for a cable when it is unknown.
- **Express spare fibers** connects matching unused numbers between two chosen cables. Existing connections are preserved.
- Record a tray, measured loss in dB, status, and notes. Existing native attributes such as splice dates and technician names survive range edits.
- Filter rows by cable, tray, type, or notes; copy the table; undo and redo edits within the dialog. Filtering only hides rows and does not remove their connections.
- Validation checks fiber counts on both cables, overlapping assignments in either direction, mismatched ranges, ambiguous cable IDs, and invalid measurements. Fix the errors before saving.

**Save** applies the table, adds newly detected enclosure memberships, and labels blank cable endpoints. Only changed splice rows are replaced; other enclosures and unchanged splice records keep their IDs and attributes. **Cancel** leaves the splice table unchanged. Changes stay in the QGIS edit buffers until you save layer edits. Each affected layer has its own undo step.

If the splice layer needs `a_layer_id`, `b_layer_id`, `tray`, `notes`, or `loss_db` fields, they are added in the same edit command, so those changes can also be undone. Stored relation tables are reopened when available, preserving their data even when they were removed from the layer tree.

Duct segments are ordered by chaining their endpoints. If they do not form one chain, reorder them in the dialog before saving. Ducts that have no segment id are stored by layer and feature id. The plugin does not invent segment ids.

### Trace selection

Select an AP, camera, jack, cable, IDF, or panel, then run **Trace selection**. The report lists the route in plain lines:

- Device
- Pole, when the device is mounted on one
- Cable id, cable type, and length
- Ducts, in order, joined by `>`
- Handholes the cable passes through, joined by `>`, using each handhole's access id or access name
- IDF
- Panel and port
- Associated fiber segments, in connection order
- Splice enclosures on that path
- Fiber strands, compressed into ranges, including splice hops

The map highlight uses orange for cables, blue for devices, green for IDFs, magenta for splice enclosures, and brown for handholes. Duct banks are named in the report and are not highlighted. If the same cable id exists more than once, every copy is highlighted and the report says so.

Trace order is the selected device first, then a selected cable, then a selected IDF or panel. A panel-port trace walks the circuit back from that port.

### Export a spreadsheet

**Export** in the OSP Report dock opens a dialog for the current trace. Check the circuits to include and the columns to write. The columns are always:

Device, Pole, Cable, Cable type, Length, Ducts, Handholes, IDF, Panel, Port, Strands, Segments, Enclosures, Warnings

Create a new `.csv` or `.xlsx` file, or add the checked rows to a file you already have. Adding rows keeps the columns already in that file, including columns you added by hand, and appends any selected column that is not there yet. Trace another circuit later and add it to the same file. The column names do not change, so the sheet can grow into one table.

CSV files are UTF-8, which lets Excel on Windows keep the ids and strand text intact. Excel workbooks are `.xlsx` and do not need a separate Python package.

### Print a circuit card

**Circuit card** in the OSP Report dock uses the current trace. Check the circuits to include. Each one becomes a page in a PDF: the device, the path from that device through the cable, handholes, and enclosures to the panel and IDF, then the pole, cable, ducts, strands, and any warnings.

The bottom of the first page is a map of the background layers and only the traced cable, device, IDF, enclosures, and handholes. Other plant stays off that map. When the project has no background layer, the map still draws those features.

Save the PDF and print it from the viewer that opens. The card does not need a separate Python package.

### Add fiber enclosure

Choose **Add fiber enclosure**, then click a point on a fiber cable, away from either end.

- A mid-span click splits that cable into two segments. The original feature keeps its cable id and becomes the piece on the near side. The far piece receives the next id for that layer.
- Every strand is expressed through on the same strand number unless you say otherwise. Expressed strands are intact glass, not a fusion splice.
- In the splice dialog, mark strands that stay open and strands that are fusion spliced. A lateral cable whose end lands on the same point can take the fusion. Example: cable A strands 47–48 fused to cable C strands 1–2, with A strands 1–46 and 49–144 expressed to the new segment on the same numbers.
- Clicking where two through cables cross splits both and tells you the enclosure has four cable ends.
- A click on an endpoint does not split that cable. It is treated as a lateral that ends at the enclosure.
- Canceling the splice dialog rolls the split back.

The two pieces of a split cable are linked to each other and to the enclosure. Tracing either segment lists the associated segments and the enclosure, and highlights them. The enclosure dialog asks for a prefix such as `VIB` or `OSP`, and the new id is the next `PREFIX-EN-` number. Lengths are recalculated only when the layer’s map units are meters.

### Add device

Choose **Add device** and click the map. The device type list is the point layers in the project that hold devices, such as AP, CCTV, and POS. The name has to be unique.

If the click is on a cable, the dialog also asks for the cable ends, the IDF, and the panel and port. A panel name that is not already a feature is created on that IDF. A pole under the click is recorded as the mount. The cable, pole, panel, and port are written before the next dialog, and those fields are not listed there. That dialog is the chosen device layer. It shows the device id you entered, and the other attributes that are still empty. FID is not listed. Leave a row blank to keep that attribute empty.

### Add cable

Choose **Add cable**. Left-click the path and right-click, or press Enter, to finish. The sketch snaps to existing geometry and can trace a duct bank, the same way the line tool does. Pick the cable layer and a unique cable id.

If the line meets one device and an IDF or a patch panel, the dialog records the ends, panel, and port the same way **Associate selection** does. When the IDF has no panel yet, the name you type is created on that IDF. A device, IDF, or splice enclosure the line meets is written onto the cable before the next dialog, and those end, panel, and port fields are not listed there. That dialog is the chosen cable layer. It shows the cable id you entered, and the other attributes that are still empty. FID is not listed. Leave a row blank to keep that attribute empty.

### Add patch panel

Choose **Add patch panel**, then click an IDF. Enter a panel id that is not already used. A new panel asks for its port count, and that count is stored on the panel along with the IDF.

### View patch panel

Choose **View patch panel** and click an IDF. The dialog asks which patch panel to view and lists each one by its id, with the panel name beside it when the panel has a name. The next dialog draws every port. Ports with a cable, device, or switch patch are blue. Click one to see the rear cable and the switch patch when that port has one, with the trace for the cable.

A copper panel has one rear cable per port. A fiber panel can have several rear cables. Each fiber cable takes a run of ports equal to its strand count when the cable does not already name ports: a 72-strand cable on ports 1-72, and the next 72-strand cable on ports 73-144. Fiber ports are paired 1-2, 3-4, and so on, unless the panel's topology attribute is `simplex`.

### View switch

Choose **View switch** and click an IDF. Pick a switch from the list, shown by switch id and name. The dialog draws that switch's ports. A patched port is blue. Click it to see the patch panel, the panel port, and the device and cable on that port when they are known.

A panel created from **Associate selection**, **Modify associations**, **Add device**, or **Add cable** asks for the port count in the same way.

### Add switch

Choose **Add switch** and click an IDF. The dialog asks for the switch id (`VIB-SW-0001`), name, port count, make, model, rack, and RU. The switch is stored on that IDF. Its ports are created from the first port id, so `gi1/0/1` with 24 ports becomes `gi1/0/1` through `gi1/0/24`.

If the project has no switch table yet, the plugin adds OSP Switches, OSP Switch Ports, and OSP Cross Connects next to the project, and relates them to each other and to the IDF.

### Patching

Choose **Patching**. Pick the IDF, then copper or fiber. Copper uses the patch panels on that IDF. Fiber uses the fiber panels. The port list shows ports that do not already have a patching record. A device or rear cable on a port does not take it off this list, so port 1 can hold a cable and a switch patch at the same time. A port that already has a patch stays off the list. Fiber is offered as odd-even pairs (`1-2`, `3-4`) unless the panel topology is `simplex` or you check **Single fiber circuit**. Then pick the switch on that IDF and one of its free ports, such as `gi1/0/3`. Click **Add patch**, then **Save**.

The patches already stored for that IDF are listed in the same dialog.

A copper cable under the click is tied to a copper panel. A fiber cable is tied to a fiber panel, and the dialog asks for the strand numbers. When both cross the same point, the dialog asks for each one. The panel is created on the matching panel layer. The dialog also asks for the cable ends and the port, the same way **Associate selection** does, and a device that sits on that cable is linked. A value that is already filled in is left as it is.

If no cable crosses the point, choose copper or fiber and the panel is still created on the IDF.

## Where the links are stored

The plugin uses the association tables the project already has. It creates a table only when that project has nowhere to put the record.

When the project already has splice, termination, route, or strand tables, those tables stay in place. The first run can add a circuits table to the cable GeoPackage, and on a cable layer that has `cable_id` but no end field it can add `a_end_type`, `a_end_id`, `z_end_type`, `z_end_id`, and `strand_count`.

When the project has none of those tables, the first **Build associations** or **Associate selection** creates `osp_links.gpkg` next to the `.qgz` and adds it to the project. Save the project before that run. The sidecar holds:

| Layer | Contents |
| --- | --- |
| OSP Fiber Strands | One row per strand |
| OSP Fiber Splices | Express, fusion, and open strand rows |
| OSP Cable Routes | Cable to duct order |
| OSP Cable Handholes | Handholes a cable passes through, in order |
| OSP Circuits | Device, IDF, panel, port, and strand range |
| OSP Splice Enclosures | The enclosure point |
| OSP Enclosure Cables | Which cables enter the enclosure |

Feature edits are one undo step per layer, named **OSP Connectivity**. The layer you just edited becomes the active layer, so **Edit → Undo** reverses that step. The features stay in the layer edit buffer until you save the layer edits. Turning editing off and discarding reverts that layer to the last save.

## How layers are recognized

Roles come from the geometry and the field names, not from the layer name. That matters when several layers share a name. Cables are lines with `cable_id`, `strand_count`, `fiber_count`, or `media_type`. Ducts are the other lines, recognized from `segment_id` or a conduit-capacity field. Points are matched to access points, cameras, panels, splice enclosures, handholes, poles, and IDFs by their id fields.

Known field names are read as aliases. `a_end_location` and `a_end_id` are both cable ends. `patch_panel_id` and `patch_pane_id` are both the panel. `strand_count` and `fiber_count` are both the strand count, and a value such as `12F` is read as 12. New ids follow the prefix already used on that layer.

## Snap tolerance

**Set snap tolerance** stores the distance in QGIS settings under `osp_connectivity/tolerance`. The default is 1 map unit, which is 1 meter when the project CRS uses meters. On a project whose units are degrees and where the setting has never been changed, the default is 0.00002. Snapping transforms into the project CRS. The plugin does not reproject the source layers.

## What this version does not do

Dark-fiber inventory, a separate duct-fill report, a standing validation dock, and copper pair counts are not in this version. A fiber layer that has no A and Z ends is reported, and the plugin does not force it into a full circuit.

The `test_qgis_editor` checks additionally exercise the real dialogs, square toolbar icons, edit rollback, and relation-table recovery when run with QGIS's bundled Python and `QT_QPA_PLATFORM=offscreen`. They skip automatically with a standard Python installation.
