// Prints what TLC's own parser reads from a .cfg, one fact a line, for
// BindConfiguration (tlc.go). Run as a single-file program against the pinned
// jar: java -cp tla2tools.jar ConfigFacts.java <file.cfg>
//
// A definition the file overrides with a value is among its constants: the
// parser cannot tell the two apart, and TLC binds the name to the spec later.

import java.util.Map;
import tlc2.tool.impl.ModelConfig;
import tlc2.util.Vect;
import util.SimpleFilenameToStream;

public class ConfigFacts {
    public static void main(String[] args) {
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
