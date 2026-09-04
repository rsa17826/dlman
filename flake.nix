{
  inputs = {
    nixpkgs = {
      url = "github:nixos/nixpkgs/nixos-unstable";
    };
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
          };

          sourceFiles = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./downloader.py
              ./native_host.py
            ];
          };

          pythonEnv = pkgs.python3.withPackages (ps: [ ps.requests ]);
        in
        {
          default = pkgs.stdenv.mkDerivation {
            pname = "_dlman";
            version = "1.0.0";
            src = sourceFiles;

            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ pythonEnv ];

            installPhase = ''
              runHook preInstall

              mkdir -p $out/bin $out/share/dlman
              cp downloader.py native_host.py $out/share/dlman/
              chmod +x $out/share/dlman/native_host.py

              makeWrapper ${pythonEnv}/bin/python $out/bin/dlman \
                --add-flags "$out/share/dlman/native_host.py" \
                --prefix PYTHONPATH : "$out/share/dlman"

              runHook postInstall
            '';
          };
        }
      );
    };
}
