use zed_extension_api as zed;

struct MaxwellExtension;

impl zed::Extension for MaxwellExtension {
    fn new() -> Self {
        MaxwellExtension
    }
}

zed::register_extension!(MaxwellExtension);
