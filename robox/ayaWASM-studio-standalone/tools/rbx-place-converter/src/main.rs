use std::io::{self, BufReader, BufWriter, Read, Write};

fn main() {
    if let Err(error) = convert(io::stdin().lock(), io::stdout().lock()) {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

fn convert<R: Read, W: Write>(input: R, output: W) -> Result<(), Box<dyn std::error::Error>> {
    let dom = rbx_binary::from_reader(BufReader::new(input))?;
    let output = BufWriter::new(output);
    let roots = dom.root().children();
    rbx_xml::to_writer_default(output, &dom, roots)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::convert;
    use rbx_dom_weak::{InstanceBuilder, WeakDom};
    use std::io::Cursor;

    #[test]
    fn converts_binary_place_to_xml() {
        let value = InstanceBuilder::new("StringValue")
            .with_name("AssetReference")
            .with_property("Value", "rbxassetid://12345");
        let dom = WeakDom::new(InstanceBuilder::new("DataModel").with_child(value));
        let mut binary = Vec::new();
        rbx_binary::to_writer(&mut binary, &dom, dom.root().children()).unwrap();

        let mut xml = Vec::new();
        convert(Cursor::new(binary), &mut xml).unwrap();
        let xml = String::from_utf8(xml).unwrap();

        assert!(xml.contains("StringValue"), "{xml}");
        assert!(xml.contains("<string name=\"Name\">AssetReference</string>"));
        assert!(xml.contains("rbxassetid://12345"));
    }
}
