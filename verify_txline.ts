/** Verify the packaged TxLINE goal record against TxLINE's devnet program. */

import * as anchor from "@coral-xyz/anchor";
import { ComputeBudgetProgram, Connection, Keypair, PublicKey } from "@solana/web3.js";
import BN from "bn.js";
import * as fs from "fs";
import idl from "./txline/txoracle.devnet.json";
import proofArtifact from "./data/proofs/txline_score_18257739_seq1188.json";

type ProofNode = { hash: number[] | string; isRightSibling: boolean };

function bytes32(value: number[] | string): number[] {
  const bytes = Array.isArray(value)
    ? Buffer.from(value)
    : Buffer.from(value.replace(/^0x/, ""), value.startsWith("0x") ? "hex" : "base64");
  if (bytes.length !== 32) throw new Error(`Expected 32-byte hash, got ${bytes.length}`);
  return Array.from(bytes);
}

function mapProof(nodes: ProofNode[]) {
  return nodes.map((node) => ({
    hash: bytes32(node.hash),
    isRightSibling: node.isRightSibling,
  }));
}

async function main() {
  const artifact = proofArtifact as any;
  const value = artifact.validation;
  if (value.summary.fixtureId !== artifact.fixtureId) {
    throw new Error("Fixture mismatch in proof artifact");
  }
  if (value.statToProve.value !== artifact.expectedValue) {
    throw new Error("Stat value mismatch in proof artifact");
  }

  const connection = new Connection("https://api.devnet.solana.com", "confirmed");
  const keypairPath = process.env.SOLANA_KEYPAIR_PATH ?? "_keys/raven-wallet.json";
  if (!fs.existsSync(keypairPath)) {
    throw new Error(
      "A funded devnet SOLANA_KEYPAIR_PATH is required for read-only simulation",
    );
  }
  const secret = JSON.parse(fs.readFileSync(keypairPath, "utf8"));
  const wallet = new anchor.Wallet(Keypair.fromSecretKey(Uint8Array.from(secret)));
  const provider = new anchor.AnchorProvider(connection, wallet, {
    commitment: "confirmed",
  });
  const program = new anchor.Program(idl as anchor.Idl, provider);
  if (program.programId.toBase58() !== artifact.programId) {
    throw new Error("IDL program ID does not match proof network");
  }

  const targetTs = value.summary.updateStats.minTimestamp;
  const epochDay = Math.floor(targetTs / 86_400_000);
  const [dailyScoresPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("daily_scores_roots"), new BN(epochDay).toArrayLike(Buffer, "le", 2)],
    program.programId,
  );
  const fixtureSummary = {
    fixtureId: new BN(value.summary.fixtureId),
    updateStats: {
      updateCount: value.summary.updateStats.updateCount,
      minTimestamp: new BN(value.summary.updateStats.minTimestamp),
      maxTimestamp: new BN(value.summary.updateStats.maxTimestamp),
    },
    eventsSubTreeRoot: bytes32(value.summary.eventStatsSubTreeRoot),
  };
  const stat = {
    statToProve: value.statToProve,
    eventStatRoot: bytes32(value.eventStatRoot),
    statProof: mapProof(value.statProof),
  };
  const predicate = {
    threshold: artifact.expectedValue,
    comparison: { equalTo: {} },
  };
  const valid = await (program.methods as any)
    .validateStat(
      new BN(targetTs),
      fixtureSummary,
      mapProof(value.subTreeProof),
      mapProof(value.mainTreeProof),
      predicate,
      stat,
      null,
      null,
    )
    .accounts({ dailyScoresMerkleRoots: dailyScoresPda })
    .preInstructions([
      ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }),
    ])
    .view();

  if (!valid) throw new Error("TxLINE on-chain validation returned false");
  console.log("PASS TxLINE on-chain score validation");
  console.log(`fixture=${artifact.fixtureId} seq=${artifact.sequence} stat=${artifact.statKey} value=${artifact.expectedValue}`);
  console.log(`program=${program.programId.toBase58()} pda=${dailyScoresPda.toBase58()}`);
}

main().catch((error) => {
  console.error("FAIL TxLINE on-chain score validation");
  console.error(error);
  process.exit(1);
});
