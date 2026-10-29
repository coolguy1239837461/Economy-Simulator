yeah standalone build of the studio hosted at https://aya.nodium.lol/

i wont give any support for this, you have an old webserver that include the basics to get it running.
it wont work on github pages or stuff don't cry in my discord if you can't configure it up (it shouldn't be that complicated).

Local asset overrides:
- Put assets in InterceptAssets/<asset-id>.<extension>.
- The server checks its root InterceptAssets folder and game-specific folders at game/games/<game>/InterceptAssets.
- Restart the webserver after adding or replacing assets.
- Roblox asset requests use a local matching ID first; other requests fall back to Roblox by default.
- For offline asset mode, start the server with AYA_ONLINE_ASSETS=0. Missing local assets then return 404 instead of contacting Roblox.

Play tab:
- Load a local .rbxl/.rbxlx place or a ZIP package. ZIP files are uploaded in 1 MiB chunks and searched for place files and files directly inside any InterceptAssets folder.
- Extracted places and assets are installed under game/games/<zip-name>/; the selected place is loaded into Aya Studio.
- Play-launched Studio hides its embedded Start Page so the Welcome homepage does not appear inside Studio.
- The compiled runtime does not expose a standalone player launch command. Use Studio's Play Solo control to start gameplay after the place loads.

Convert tab:
- The Convert tab scans .rbxlx files and converts binary .rbxl places to XML locally before scanning supported instance properties and script source.
- Binary conversion requires the current stable Rust/Cargo toolchain. From the standalone directory, run `cargo build --release --manifest-path tools/rbx-place-converter/Cargo.toml` once; the server uses the resulting local helper. Set `AYA_RBX_CONVERTER` to use a helper at another path.
- The converter uses the MIT-licensed `rbx-dom` project (https://github.com/rojo-rbx/rbx-dom).
- Converted place files up to 1 GiB are uploaded in 1 MiB chunks to work through servers with per-request upload limits, then reassembled on disk.
- Collecting assets requires an online server and downloads only assets accessible without account cookies.
- Downloaded places are saved under game/games/<place-name>/ with InterceptAssets/manifest.json and asset-report.json, then packaged as a ZIP.
- Extract the ZIP under game/games to use the local-first asset lookup.
- Script-generated IDs and assets created at runtime are not found by this static scan.
- The server root opens the Welcome page; use its Launch Studio link or /index.html for the regular Studio app.
