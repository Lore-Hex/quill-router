// Prints what TLC's own parser reads from a .cfg, one fact a line, for
// BindConfiguration (tlc.go). Run as a single-file program against the pinned
// jar: java -cp tla2tools.jar ConfigFacts.java <file.cfg>
//
// A definition the file overrides with a value is among its constants: the
// parser cannot tell the two apart, and TLC binds the name to the spec later.
// Each of the parser's keywords is handled here, its settings printed; a
// keyword a later jar adds is printed as unhandled, for BindConfiguration to
// refuse, rather than read past.

import java.util.Map;
import java.util.Set;
import tlc2.tool.impl.ModelConfig;
import tlc2.util.Vect;
import util.SimpleFilenameToStream;

public class ConfigFacts {
    // The keywords whose settings main prints.
    private static final Set<String> HANDLED = Set.of(
        "CONSTANT", "CONSTANTS", "CONSTRAINT", "CONSTRAINTS", "ACTION_CONSTRAINT", "ACTION_CONSTRAINTS",
        "INVARIANT", "INVARIANTS", "INIT", "NEXT", "VIEW", "SYMMETRY", "SPECIFICATION", "PROPERTY",
        "PROPERTIES", "ALIAS", "POSTCONDITION", "POSTCONDITIONS", "_PERIODIC", "_RL_REWARD", "_POSSIBLE",
        "CHECK_DEADLOCK");

    public static void main(String[] args) {
        for (String keyword : ModelConfig.ALL_KEYWORDS) {
            if (!HANDLED.contains(keyword)) {
                System.out.println("unhandled\t" + keyword);
            }
        }
        ModelConfig config = new ModelConfig(args[0], new SimpleFilenameToStream());
        config.parse();
        line("spec", config.getSpec());
        line("init", config.getInit());
        line("next", config.getNext());
        Vect<?> constants = config.getConstants();
        for (int i = 0; i < constants.size(); i++) {
            // The name, its parameters if it has any, and its value.
            Vect<?> entry = (Vect<?>) constants.elementAt(i);
            System.out.println("constant\t" + entry.elementAt(0) + "\t" + (entry.size() - 2));
        }
        for (Map.Entry<String, String> e : config.getOverrides().entrySet()) {
            System.out.println("override\t" + e.getKey() + "\t" + e.getValue());
        }
        System.out.println("modconstants\t" + config.getModConstants().size());
        System.out.println("modoverrides\t" + config.getModOverrides().size());
        names("constraint", config.getConstraints());
        names("actionconstraint", config.getActionConstraints());
        line("symmetry", config.getSymmetry());
        line("view", config.getView());
        line("alias", config.getAlias());
        line("periodic", config.getPeriodic());
        line("rlreward", config.getRLReward());
        names("postcondition", config.getPostConditions());
        names("possible", config.getPossible());
        System.out.println("checkdeadlock\t" + config.getCheckDeadlock());
        names("invariant", config.getInvariants());
        names("property", config.getProperties());
    }

    private static void line(String key, String value) {
        if (value != null && !value.isEmpty()) {
            System.out.println(key + "\t" + value);
        }
    }

    private static void names(String key, Vect<?> values) {
        for (int i = 0; i < values.size(); i++) {
            System.out.println(key + "\t" + values.elementAt(i));
        }
    }
}
