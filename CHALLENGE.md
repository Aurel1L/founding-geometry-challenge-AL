# Founding Geometry Engineer: the challenge

This document is everything you need to know to do the challenge. You do not need any prior knowledge of Faber. If something is unclear, write to rodrigue@faber.ai.

## About Faber

Faber builds artificial engineers: AI agents that carry out complex mechanical engineering tasks. Our goal is to bring the design and manufacture of a mechanical part down from months to weeks, by automating the repetitive execution work so that engineers spend their time on design and judgment.

When our agent creates or modifies a part, it does so by writing code in [build123d](https://build123d.readthedocs.io), an open-source Python library for CAD. build123d runs on OpenCascade, an open-source geometry kernel: the library that actually computes extrusions, booleans and fillets on solids.

## Three representations of a part

A CAD part can be described in three ways.

**The script.** A build123d program, with variables, loops, functions and computed values. It is what our agent writes, reads and changes. Running it produces the solid.

**The feature tree.** In CAD software such as SolidWorks, Fusion, FreeCAD or Onshape, a part is shown as an ordered list of operations, each with its parameters: a sketch, an extrusion of 15 mm, a fillet of radius 2 mm on some edges, a hole of diameter 8 mm. The user edits a parameter and the software replays the list to rebuild the part. A build123d script describes the same kind of construction, but as a Python program, and once it has run, build123d keeps nothing of it.

**The B-rep.** The solid itself: its faces, its edges, and the exact surfaces and curves behind them. It is what OpenCascade computes and what the viewer shows. It holds the result, not the operations that produced it. STEP, the standard exchange format between CAD tools, stores only this.

The script and the feature tree describe how the part was built. The B-rep describes what it is.

## The problem

Code suits the agent. It can write it, read it and change it like any other program. It suits the engineer much less. An engineer checking a part wants to click on it and fix it the way they would in CAD software, not edit Python.

The part is not always right the first time: a chamfer on the wrong edge, a fillet on the wrong corner, a face a few millimetres off. The fastest fix is direct modeling, which edits the solid itself with no history involved: you push or pull a face and the neighbouring faces follow, you move a chamfer or a fillet to another edge. Fusion's direct mode, Ansys SpaceClaim and Siemens' Synchronous Technology work this way.

Then the agent can keep working on the fixed part: it adds a feature, changes a dimension, prepares the part for machining. This is where it breaks. The agent reads the script, but the script no longer describes the part. After a manual edit, the part is a script, plus a sequence of manual operations, plus the B-rep they produce together. The agent only sees the first.

**The question behind this challenge: how do you represent a part edited by hand, so that the agent can perceive and understand its state, and keep working on it?**

## What we ask

1. **Direct editing.** Add tools to the viewer to edit a part by hand: offset a face, and add, move or remove a chamfer or a fillet. "By hand" means through clicks in the viewer, not by editing the script in the editor.
2. **An agent that understands the edited part.** After these edits, the agent must be able to tell what the engineer changed and what the part is now, and keep working on it: add a hole, change a dimension, without losing the engineer's edits.
3. **Bonus: a part with no history.** Do the same on a part that has no script. To get one, export any example to STEP with build123d's `export_step()` and load it back with `import_step()`. The part is then a B-rep plus manual operations, and nothing else.

A few precisions on what we mean.

- How you represent the edited part is the heart of the challenge, and your call. The script, the feature tree and the B-rep are all candidates, alone or combined.
- The agent here is any coding agent connected to the workbench through its MCP server (see below). Its tools today only see the script, the build result and the user's selection. Whatever you decide the state of the part is, the agent needs a way to read it.

## What we provide

This repository contains a small workbench built on the libraries we use (build123d, ocp-tessellate, three-cad-viewer):

- a web page with an editor on `model.py` next to a 3D view of what it builds, which reports the face or edge you click;
- an MCP server, so that a coding agent such as Claude Code can read and write the script, run it, and read what the user selected in the viewer.

The [README](README.md) explains how to run it. It is a starting point, not a constraint. You are free to change absolutely anything: the viewer, the editor, the HTTP API, the MCP server and its tools, the way scripts are run. Rewrite or replace any part of it if your solution needs it.

The `examples/` folder holds twelve real parts written in build123d, taken from the build123d documentation: the Too Tall Toby modeling exercises. Use them to try your tools.

## Practical

- **Tools.** Any language and any library. AI coding assistants are allowed, and we expect you to use them. In the review we ask about your choices, not about who typed the code.
- **What to hand in.** A Git repository with instructions to run it. Start it with "Use this template" on GitHub and keep it private, then send rodrigue@faber.ai either a link with access granted or an archive.
- **The review.** One hour with the team: a demo of your work, then a discussion of your choices.

## What we look at

There is no ideal solution, and we do not have one either. What we look at is how deeply you understand the problem, your vision of where it should go, and how quickly you find solutions that hold.

How you think about the representation of the part, and how clearly you see what is hard. How many operations your tools cover matters less.

We do not expect a polished interface, support for freeform surfaces, or coverage of every build123d feature. If your solution breaks somewhere, showing where and explaining why is worth more than hiding it.
