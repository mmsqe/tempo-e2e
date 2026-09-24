// Pull the initcode of the Hyperlane contracts the bridge suite deploys out of an unpacked
// @hyperlane-xyz/core package. Its typechain factories carry the bytecode as published.
// Usage: node hyperlane-artifacts.js <unpacked package dir> <version> > hyperlane.json
const fs = require("fs");
const path = require("path");

const [dir, version] = process.argv.slice(2);
const factories = path.join(dir, "dist/typechain/factories/contracts");
const contracts = {
  Mailbox: "Mailbox__factory.js",
  MerkleTreeHook: "hooks/MerkleTreeHook__factory.js",
  PausableHook: "hooks/PausableHook__factory.js",
  ValidatorAnnounce: "isms/multisig/ValidatorAnnounce__factory.js",
  StaticMessageIdMultisigIsmFactory:
    "isms/multisig/StaticMultisigIsm.sol/StaticMessageIdMultisigIsmFactory__factory.js",
  HypERC20Collateral: "token/HypERC20Collateral__factory.js",
  HypERC20: "token/HypERC20__factory.js",
};

const bytecode = {};
for (const [name, file] of Object.entries(contracts)) {
  const src = fs.readFileSync(path.join(factories, file), "utf8");
  bytecode[name] = src.match(/const _bytecode = "(0x[0-9a-f]+)"/)[1];
}
process.stdout.write(
  JSON.stringify(
    {
      source: `npm:@hyperlane-xyz/core@${version}`,
      note: "Regenerate with: make hyperlane-artifacts",
      deployer_bytecode: bytecode,
    },
    null,
    1,
  ) + "\n",
);
